# -*- coding: utf-8 -*-
"""
Gustavo worker: recibe el tick de WordPress, usa Gemini u OpenAI, ejecuta acciones (inbox, foros) y registra en WP.
Variables de entorno:
  GUSTAVO_SECRET     = misma API key que en WordPress (para aceptar el POST)
  GUSTAVO_WP_URL    = https://tulicenciadeapertura.es (sin barra final)
  GUSTAVO_WP_API_KEY = misma API key (para enviar a /log, send-email, etc.)
  GEMINI_API_KEY    = API key de Google AI Studio (gratis)
  OPENAI_API_KEY    = opcional; si está definida, se usa OpenAI en lugar de Gemini (de pago, céntimos/mes)
"""
import json
import os
import re
import secrets
import requests
from urllib.parse import urlparse, urljoin
from flask import Flask, request, jsonify
import google.generativeai as genai

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

app = Flask(__name__)

GUSTAVO_SECRET = os.environ.get("GUSTAVO_SECRET", "").strip()
GUSTAVO_WP_URL = os.environ.get("GUSTAVO_WP_URL", "").rstrip("/")
GUSTAVO_WP_API_KEY = os.environ.get("GUSTAVO_WP_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {GUSTAVO_WP_API_KEY}",
}


def check_auth():
    """Acepta el token si coincide con GUSTAVO_SECRET o con GUSTAVO_WP_API_KEY (misma clave en ambos sitios)."""
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:].strip()
    if not token:
        return False
    if GUSTAVO_SECRET and token == GUSTAVO_SECRET:
        return True
    if GUSTAVO_WP_API_KEY and token == GUSTAVO_WP_API_KEY:
        return True
    return False


def check_auth_from_body(body):
    """Acepta la clave si viene en el JSON body (por si un proxy quita el header Authorization)."""
    if not body or not isinstance(body, dict):
        return False
    token = (body.get("api_key") or "").strip()
    if not token:
        return False
    if GUSTAVO_SECRET and token == GUSTAVO_SECRET:
        return True
    if GUSTAVO_WP_API_KEY and token == GUSTAVO_WP_API_KEY:
        return True
    return False


def wp_get(url):
    if not GUSTAVO_WP_URL or not GUSTAVO_WP_API_KEY:
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def wp_post(url, data):
    if not GUSTAVO_WP_URL or not GUSTAVO_WP_API_KEY:
        return False
    try:
        r = requests.post(url, json=data, headers=HEADERS, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def send_to_wp_log(channel, type_, target, content, link="", status="done", meta=None):
    data = {
        "channel": channel,
        "type": type_,
        "target": target,
        "content": content,
        "link": link or "",
        "status": status,
    }
    if meta is not None:
        data["meta"] = meta
    return wp_post(f"{GUSTAVO_WP_URL}/wp-json/gustavo/v1/log", data)


def parse_gemini_action(text):
    """Extrae canal, tipo, destino, contenido, enlace y opcionales ASUNTO, DESTINO_EMAIL, FORUM_URL, etc."""
    channel = "outreach"
    type_ = "email"
    target = "medio/colegio"
    content = text
    link = ""
    asunto = ""
    destino_email = ""
    forum_url = ""
    thread_url = ""
    for line in text.split("\n"):
        line_stripped = line.strip()
        if line_stripped.lower().startswith("canal:"):
            channel = line_stripped.split(":", 1)[1].strip()[:50]
        elif line_stripped.lower().startswith("tipo:"):
            type_ = line_stripped.split(":", 1)[1].strip()[:50]
        elif line_stripped.lower().startswith("destino:"):
            target = line_stripped.split(":", 1)[1].strip()[:200]
        elif line_stripped.lower().startswith("enlace:"):
            link = line_stripped.split(":", 1)[1].strip()[:500]
        elif line_stripped.lower().startswith("asunto:"):
            asunto = line_stripped.split(":", 1)[1].strip()[:300]
        elif line_stripped.lower().startswith("destino_email:") or line_stripped.lower().startswith("destino email:"):
            destino_email = line_stripped.split(":", 1)[1].strip()[:255]
        elif line_stripped.lower().startswith("forum_url:") or line_stripped.lower().startswith("forum url:"):
            forum_url = line_stripped.split(":", 1)[1].strip()[:500]
        elif line_stripped.lower().startswith("thread_url:") or line_stripped.lower().startswith("thread url:"):
            thread_url = line_stripped.split(":", 1)[1].strip()[:500]
    url_match = re.search(r"https?://[^\s\)]+", text)
    if not link and url_match:
        link = url_match.group(0)
    content_clean = re.sub(
        r"^(canal|tipo|destino|enlace|asunto|destino_email|destino email|forum_url|forum url|thread_url|thread url):.*$",
        "", text, flags=re.I | re.M
    ).strip()
    if content_clean:
        content = content_clean
    return {
        "channel": channel,
        "type": type_,
        "target": target,
        "content": content[:50000],
        "link": link,
        "asunto": asunto,
        "destino_email": destino_email.strip().lower() if destino_email else "",
        "forum_url": forum_url,
        "thread_url": thread_url,
    }


def should_skip_email(email_no_responder, from_email, subject):
    """True si no debemos contestar (factura, etc.)."""
    if not email_no_responder:
        return False
    combined = (from_email or "") + " " + (subject or "")
    combined_lower = combined.lower()
    for pattern in email_no_responder:
        if pattern and pattern.lower() in combined_lower:
            return True
    return False


def _extract_domain(url):
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    return urlparse(url).netloc.lower().replace("www.", "")


def _gen_password(length=14):
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def try_register_forum(base_url, email="hola@tulicenciadeapertura.es"):
    """
    Intenta registrarse en un foro/sitio: busca página de registro, rellena formulario con
    usuario Gustavo/gustavo_tulicencia y contraseña generada. Devuelve (username, password)
    si parece que el registro funcionó, o None si no hay formulario, hay captcha o hace falta
    verificación por email.
    """
    if not sync_playwright:
        return None
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    username = "Gustavo"
    password = _gen_password()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            context.set_default_timeout(12000)
            page = context.new_page()
            page.goto(base_url, wait_until="domcontentloaded", timeout=12000)

            # Buscar enlace a registro
            signup_links = page.query_selector_all('a[href*="register"], a[href*="registro"], a[href*="signup"], a[href*="crear"], a[href*="unirse"], a[href*="join"]')
            signup_href = None
            for a in signup_links:
                href = a.get_attribute("href") or ""
                if href and "login" not in href.lower() and "entrar" not in href.lower():
                    signup_href = href
                    break
            if signup_href:
                page.goto(urljoin(base_url, signup_href), wait_until="domcontentloaded", timeout=12000)
            else:
                for path in ["/register", "/registro", "/signup", "/user/register", "/foro/register", "/forum/register", "/registrarse"]:
                    try:
                        page.goto(urljoin(base_url, path), wait_until="domcontentloaded", timeout=8000)
                        if page.url and "register" in page.url or "registro" in page.url or "signup" in page.url:
                            break
                    except Exception:
                        continue

            pass_input = page.query_selector('input[type="password"]')
            if not pass_input:
                browser.close()
                return None
            # Segundo password (confirmación) a veces existe; lo rellenamos igual
            pass_inputs = page.query_selector_all('input[type="password"]')
            user_input = (
                page.query_selector('input[name="username"]')
                or page.query_selector('input[name="user"]')
                or page.query_selector('input[name="login"]')
                or page.query_selector('input[id="username"]')
                or page.query_selector('input[type="text"]')
            )
            email_input = (
                page.query_selector('input[name="email"]')
                or page.query_selector('input[type="email"]')
            )
            if not user_input:
                browser.close()
                return None
            user_input.fill(username)
            for inp in pass_inputs:
                inp.fill(password)
            if email_input:
                email_input.fill(email)
            submit = (
                page.query_selector('input[type="submit"]')
                or page.query_selector('button[type="submit"]')
                or page.query_selector('button:has-text("Registr")')
                or page.query_selector('button:has-text("Sign up")')
                or page.query_selector('button:has-text("Crear")')
                or page.query_selector('input[value="Registr"]')
                or page.query_selector('input[value="Sign up"]')
            )
            if not submit:
                browser.close()
                return None
            submit.click()
            page.wait_for_timeout(4000)
            new_url = page.url
            content_lower = (page.content() or "").lower()
            if "captcha" in content_lower or "verificar" in content_lower and "email" in content_lower or "confirm your email" in content_lower or "comprueba tu" in content_lower:
                browser.close()
                return None
            if "logout" in content_lower or "salir" in content_lower or "cerrar sesión" in content_lower or "mi cuenta" in content_lower or "perfil" in content_lower:
                browser.close()
                return (username, password)
            if new_url and new_url != base_url and "register" not in new_url and "registro" not in new_url and "signup" not in new_url:
                browser.close()
                return (username, password)
            browser.close()
            return None
    except Exception:
        return None


def try_post_forum(post_url, content, username, password):
    """Intenta publicar en un foro con Playwright: login si hace falta, rellenar respuesta y enviar. Devuelve True si parece que se publicó."""
    if not sync_playwright or not username or not content:
        return False
    if not post_url.startswith("http"):
        post_url = "https://" + post_url
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            context.set_default_timeout(15000)
            page = context.new_page()
            page.goto(post_url, wait_until="domcontentloaded", timeout=15000)

            # ¿Hay formulario de login?
            pass_input = page.query_selector('input[type="password"]')
            if pass_input:
                user_input = (
                    page.query_selector('input[name="username"]')
                    or page.query_selector('input[name="log"]')
                    or page.query_selector('input[name="email"]')
                    or page.query_selector('input[id="user_login"]')
                    or page.query_selector('input[type="text"]')
                )
                if user_input and password:
                    user_input.fill(username)
                    pass_input.fill(password)
                    submit = (
                        page.query_selector('input[type="submit"]')
                        or page.query_selector('button[type="submit"]')
                        or page.query_selector('button:has-text("Entrar")')
                        or page.query_selector('button:has-text("Login")')
                        or page.query_selector('button:has-text("Iniciar")')
                        or page.query_selector('input[value="Entrar"]')
                        or page.query_selector('input[value="Login"]')
                    )
                    if submit:
                        submit.click()
                        page.wait_for_load_state("networkidle", timeout=10000)
                page.goto(post_url, wait_until="domcontentloaded", timeout=15000)

            # Buscar textarea de respuesta (nombre común: message, reply, content, comentario)
            textarea = (
                page.query_selector('textarea[name="message"]')
                or page.query_selector('textarea[id="message"]')
                or page.query_selector('textarea[name="reply"]')
                or page.query_selector('textarea[name="content"]')
                or page.query_selector('textarea[name="comentario"]')
                or page.query_selector('textarea.msg')
                or page.query_selector("textarea")
            )
            if not textarea:
                browser.close()
                return False
            textarea.fill(content[:50000])

            # Botón de enviar
            submit_btn = (
                page.query_selector('input[type="submit"][value*="Responder"]')
                or page.query_selector('input[type="submit"][value*="Enviar"]')
                or page.query_selector('input[type="submit"][value*="Post"]')
                or page.query_selector('input[type="submit"][value*="Publicar"]')
                or page.query_selector('button:has-text("Responder")')
                or page.query_selector('button:has-text("Enviar")')
                or page.query_selector('button:has-text("Publicar")')
                or page.query_selector('button:has-text("Post")')
                or page.query_selector('input[type="submit"]')
                or page.query_selector('button[type="submit"]')
            )
            if not submit_btn:
                browser.close()
                return False
            submit_btn.click()
            page.wait_for_timeout(3000)
            browser.close()
            return True
    except Exception:
        return False


def llm_generate(prompt):
    """Genera texto con Gemini o, si está definido OPENAI_API_KEY, con OpenAI (gpt-4o-mini)."""
    if OPENAI_API_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=OPENAI_API_KEY)
            r = client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
            )
            if r.choices and r.choices[0].message.content:
                return r.choices[0].message.content.strip()
        except Exception:
            pass
        return ""
    if GEMINI_API_KEY:
        try:
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(prompt)
            return (response.text or "").strip()
        except Exception:
            pass
    return ""


@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "gustavo-worker"})


@app.route("/", methods=["POST"])
@app.route("/api/gustavo", methods=["POST"])
def tick():
    body = request.get_json() or {}
    if not check_auth() and not check_auth_from_body(body):
        auth_header = (request.headers.get("Authorization") or "").strip()
        header_token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        debug = {
            "received_header_len": len(header_token),
            "body_has_api_key": bool(body.get("api_key")),
        }
        return jsonify({"error": "Unauthorized", "debug": debug}), 401
    if not GEMINI_API_KEY and not OPENAI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY or OPENAI_API_KEY required"}), 500
    limits = body.get("limits", {})
    used = limits.get("used_today", {})
    max_foros = limits.get("max_foros_day", 28)
    max_emails = limits.get("max_emails_day", 35)
    inbox_url = body.get("inbox_url", "")
    send_email_url = body.get("send_email_url", "")
    mark_replied_url = body.get("mark_replied_url", "")
    queue_forum_post_url = body.get("queue_forum_post_url", "")
    forum_accounts_url = body.get("forum_accounts_url", "")
    pending_forum_patch_url = body.get("pending_forum_patch_url", "")

    instructions_block = body.get("gustavo_instructions", {})
    instructions_text = instructions_block.get("instructions", "")
    site_url = body.get("site_url", "")
    message_admin = body.get("message_from_admin", "")
    email_no = body.get("email_no_responder", [])
    coverage = body.get("coverage", {}) or {}
    url_rule = body.get("url_rule", "")
    url_examples = body.get("url_examples", []) or []
    client_journey = body.get("client_journey", "")
    needs_account_url = body.get("needs_account_url", "")
    outbound_email = body.get("outbound_email", "hola@tulicenciadeapertura.es")

    # --- Prioridad 1: contestar bandeja de entrada ---
    if inbox_url and send_email_url and mark_replied_url and used.get("email", 0) < max_emails:
        inbox_data = wp_get(inbox_url)
        messages = (inbox_data or {}).get("messages", [])
        for msg in messages:
            from_email = (msg.get("from_email") or "").strip()
            subject = (msg.get("subject") or "").strip()
            if should_skip_email(email_no, from_email, subject):
                continue
            body_text = msg.get("body_text") or msg.get("body_html") or ""
            body_html = msg.get("body_html") or ""
            thread_id = msg.get("thread_id", "")
            msg_id = msg.get("id")

            url_context = ""
            if url_rule:
                url_context += f"\n{url_rule}\n"
            if url_examples:
                samples = url_examples[:80]
                url_context += f"\nURLs que SÍ existen (solo enlaza a URLs de esta lista; nunca construyas ni inventes una): {', '.join(samples)}\n"
            if client_journey:
                url_context += f"\n{client_journey}\n"

            reply_prompt = f"""Eres Gustavo, del equipo de tulicenciadeapertura.es. Sigue SIEMPRE estas instrucciones:

{instructions_text}
{url_context}

Patrones de email a los que NO responder (si encajan, no respondas): {email_no}

Has recibido este correo:
De: {from_email}
Asunto: {subject}

Cuerpo:
{body_text[:8000]}

Responde como un profesional humano, útil y breve. Si preguntan cómo contactar con un técnico o asesoramiento, indica la ruta exacta (entrar en la web, buscador, elegir servicio y municipio, contactar desde ahí) y enlaza a la URL que corresponda. Si es otra consulta sobre servicios, trámites o colaboración, ayuda y enlaza a la plataforma cuando venga al caso. No admitas ser IA. Escribe solo el cuerpo del correo de respuesta (sin asunto ni cabeceras). Máximo 150 palabras."""

            reply_body = llm_generate(reply_prompt)

            if reply_body and from_email:
                re_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
                sent = wp_post(send_email_url, {
                    "to": from_email,
                    "subject": re_subject[:200],
                    "body": reply_body,
                })
                if sent:
                    wp_post(mark_replied_url, {"id": msg_id})
                    send_to_wp_log("email", "inbox_reply", from_email, reply_body, site_url, "done")
                    if message_admin and GUSTAVO_WP_URL and GUSTAVO_WP_API_KEY:
                        try:
                            requests.post(
                                f"{GUSTAVO_WP_URL}/wp-json/gustavo/v1/clear-message",
                                headers=HEADERS,
                                timeout=10,
                            )
                        except Exception:
                            pass
                    return jsonify({"ok": True, "action": "inbox_reply", "to": from_email})
            break

    # --- Prioridad 2: outreach solo en foros (España); sin emails fríos ---
    if used.get("foros", 0) >= max_foros:
        return jsonify({"ok": True, "skipped": "limits_reached"})

    coverage_note = "Lista de (esp_slug, mun_id, mun_nombre) donde SÍ hay profesionales. Solo enlaza a un pueblo concreto si está en esta lista; si no, usa " + site_url
    coverage_json = json.dumps(coverage, ensure_ascii=False)[:4000]

    url_outreach = ""
    if url_rule:
        url_outreach += f"\n{url_rule}\n"
    if url_examples:
        samples = url_examples[:100]
        url_outreach += f"URLs que SÍ existen (ENLACE solo puede ser una de estas; nunca inventes una URL): {', '.join(samples)}\n"

    prompt = f"""Eres Gustavo. Sigue SIEMPRE estas instrucciones:

{instructions_text}
{url_outreach}

Mensaje del administrador: {message_admin or 'Ninguno.'}
Patrones a no responder en bandeja: {email_no}

ÁMBITO: Solo España (foros, sitios y audiencia española o que necesite servicios en España). No actúes fuera de este ámbito.

COBERTURA: {coverage_note}
{coverage_json}

Tarea: genera UNA respuesta para un foro (o sitio donde dejar mensaje) en España. Ejemplo: alguien pregunta por licencia de apertura, trámites, reformas, instalaciones, etc. Responde con valor, enlaza a la plataforma cuando cierre bien: elige una URL de la lista url_examples que encaje con el contexto (y que esté en cobertura); si no hay ninguna que encaje, usa {site_url}. Nunca inventes una URL. Máximo 100 palabras. Elige tú el contexto (autónomos, hostelería, instaladores, arquitectos, energía solar, etc.).

Formato de respuesta (al final del texto):
CANAL: foros
TIPO: foro_respuesta
DESTINO: nombre o descripción del foro/sitio (ej. "foro autónomos Madrid")
ENLACE: (URL concreta de la plataforma, p. ej. página servicio+municipio si aplica, o home)
FORUM_URL: (url del foro o sitio, si la conoces o es realista)
THREAD_URL: (url del hilo si aplica; puede quedar vacío)

Escribe el contenido útil primero y las líneas CANAL/TIPO/DESTINO/ENLACE/FORUM_URL/THREAD_URL al final."""

    text = llm_generate(prompt)
    if not text:
        return jsonify({"ok": True, "skipped": "empty_response"})

    needs_match = re.search(r"NEEDS_ACCOUNT\s*:\s*(\S+)", text, re.I)
    if needs_match and needs_account_url and GUSTAVO_WP_API_KEY:
        domain = needs_match.group(1).strip().strip(".,")
        if domain and len(domain) < 200:
            try:
                wp_post(needs_account_url, {"domain": domain, "reason": "registro no posible (captcha/verificación)"})
            except Exception:
                pass

    parsed = parse_gemini_action(text)
    channel = parsed["channel"]
    type_ = parsed["type"]
    target = parsed["target"]
    content = parsed["content"]
    link = parsed["link"] or site_url
    forum_url = (parsed["forum_url"] or "").strip()
    thread_url = (parsed["thread_url"] or "").strip()
    post_url = thread_url or forum_url or ""

    # Encolar siempre (registro); luego intentar publicación automática si hay cuenta
    queue_id = None
    if queue_forum_post_url:
        r = requests.post(
            queue_forum_post_url,
            json={
                "forum_name": target,
                "forum_url": forum_url,
                "thread_url": thread_url,
                "content": content,
            },
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            queue_id = data.get("id")

    posted = False
    if post_url and forum_accounts_url and sync_playwright and queue_id and pending_forum_patch_url:
        domain = urlparse(post_url if post_url.startswith("http") else "https://" + post_url).netloc or ""
        domain = domain.lower().replace("www.", "")
        base_url = "https://" + domain if domain else ""
        outbound_email = body.get("outbound_email", "hola@tulicenciadeapertura.es")
        if domain:
            accounts_data = wp_get(forum_accounts_url)
            accounts = (accounts_data or {}).get("accounts", [])
            creds = next((a for a in accounts if (a.get("domain") or "").lower().replace("www.", "") == domain), None)
            if not creds and base_url:
                reg = try_register_forum(base_url, email=outbound_email)
                if reg:
                    reg_username, reg_password = reg
                    wp_post(forum_accounts_url, {"domain": domain, "username": reg_username, "password": reg_password})
                    creds = {"domain": domain, "username": reg_username, "password": reg_password}
            if creds:
                if try_post_forum(post_url, content, creds.get("username", ""), creds.get("password", "")):
                    posted = True
                    if GUSTAVO_WP_URL and GUSTAVO_WP_API_KEY:
                        try:
                            requests.patch(
                                f"{GUSTAVO_WP_URL}/wp-json/gustavo/v1/pending-forum-posts/{queue_id}",
                                json={"status": "done"},
                                headers=HEADERS,
                                timeout=10,
                            )
                        except Exception:
                            pass
            elif domain and needs_account_url:
                try:
                    wp_post(needs_account_url, {"domain": domain, "reason": "registro automático no posible (captcha/verificación o formulario no soportado)"})
                except Exception:
                    pass

    send_to_wp_log(channel, type_, target, content, link, "done", meta={"posted_auto": posted})

    if message_admin and GUSTAVO_WP_URL and GUSTAVO_WP_API_KEY:
        try:
            requests.post(f"{GUSTAVO_WP_URL}/wp-json/gustavo/v1/clear-message", headers=HEADERS, timeout=10)
        except Exception:
            pass

    return jsonify({"ok": True, "logged": True, "action": type_})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
