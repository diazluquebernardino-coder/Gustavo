# Cómo conectar Gustavo con Gemini (gratis)

## 1. Obtener API key de Gemini (gratis)

1. Entra en **Google AI Studio**: https://aistudio.google.com/
2. Inicia sesión con tu cuenta de Google.
3. Menú (≡) → **Get API key** → **Create API key** (puedes usar un proyecto nuevo o uno existente).
4. Copia la clave y guárdala en un sitio seguro. **No la compartas ni la subas a ningún sitio público.**

El plan gratuito de Gemini permite un volumen de solicitudes suficiente para que Gustavo trabaje cada 15 minutos sin coste. Si en el futuro te pasas de cuota, Google te avisará; hasta entonces es 0 €.

---

## 2. Dónde ejecutar el worker (Gustavo “cerebro”)

El archivo `main.py` de esta carpeta es el que recibe las llamadas de WordPress y usa Gemini. Necesitas ejecutarlo en un sitio con URL pública. Opciones:

### Opción A – Railway (recomendada, gratis al inicio)

1. Entra en https://railway.app y regístrate (con GitHub).
2. **New Project** → **Deploy from GitHub repo**. Si no tienes repo, crea uno con solo esta carpeta `gustavo-worker` (requirements.txt + main.py).
3. En el proyecto, **Variables** y añade:
   - `GUSTAVO_SECRET` = una contraseña larga que tú inventes (la misma que pondrás en WordPress como “API key”).
   - `GUSTAVO_WP_URL` = `https://tulicenciadeapertura.es`
   - `GUSTAVO_WP_API_KEY` = la misma que `GUSTAVO_SECRET` (la que WordPress usa para aceptar los logs).
   - `GEMINI_API_KEY` = la API key de Gemini del paso 1.
4. Railway te dará una URL tipo `https://tu-proyecto.up.railway.app`. Esa es tu **Endpoint**.

### Opción B – Render (gratis)

1. https://render.com → regístrate.
2. **New** → **Web Service** → conecta el repo con `gustavo-worker` (o sube el código).
3. Build command: `pip install -r requirements.txt`
4. Start command: `python main.py`
5. En **Environment** añade las mismas variables que arriba (`GUSTAVO_SECRET`, `GUSTAVO_WP_URL`, `GUSTAVO_WP_API_KEY`, `GEMINI_API_KEY`).
6. La URL que te dé Render (ej. `https://gustavo-xxxx.onrender.com`) es tu **Endpoint**.

### Opción C – Tu propio servidor o VPS

En la carpeta del worker:

```bash
pip install -r requirements.txt
export GUSTAVO_SECRET="tu_clave_secreta_larga"
export GUSTAVO_WP_URL="https://tulicenciadeapertura.es"
export GUSTAVO_WP_API_KEY="tu_clave_secreta_larga"
export GEMINI_API_KEY="tu_api_key_de_google"
python main.py
```

Después pon detrás de un proxy (nginx, etc.) o usa un túnel (ngrok) para exponer la URL que WordPress llamará.

---

## 3. Configurar WordPress

1. En tu WordPress: **Gustavo → Configuración**.
2. **Endpoint (URL)**: la URL pública del worker (ej. `https://tu-proyecto.up.railway.app` o `https://tu-proyecto.up.railway.app/api/gustavo` si usas esa ruta).
3. **API key / token**: la misma clave que pusiste en `GUSTAVO_SECRET` y `GUSTAVO_WP_API_KEY`.
4. Guarda. Asegúrate de que **Gustavo activo** esté marcado y el horario sea el que quieras (ej. 08–22).

A partir de ahí, cada 15 minutos (en horario activo) WordPress enviará un POST a tu worker; el worker usará Gemini para generar una acción (email o respuesta de foro), la enviará a tu web con `/gustavo/v1/log` y la verás en **Gustavo → Actividad**.

---

## 4. Cuentas en foros / medios

El worker no crea cuentas en foros ni en medios; solo genera textos y los registra en tu panel. Para que Gustavo *publique* en un sitio concreto hace falta que ese sitio tenga ya una cuenta creada (tú la creas una vez) y que un servicio futuro use esas credenciales para publicar. Este worker se limita a: recibir el tick, hablar con Gemini y guardar en WordPress lo que Gustavo “haría”, para que tú lo revises o para que otro proceso lo publique después. Así evitas gastar y mantienes control: primero pruebas que todo funcione con Gemini y el panel; luego, si quieres, se puede añadir publicación automática donde tengas cuenta.

---

## Resumen

- **Gemini**: gratis en Google AI Studio, API key en un minuto.
- **Worker**: `main.py` + `requirements.txt` desplegados en Railway, Render o tu servidor, con las 4 variables de entorno.
- **WordPress**: Endpoint = URL del worker, API key = la misma que `GUSTAVO_SECRET`.
- **Cuentas**: las creas tú cuando haga falta; Gustavo usa las instrucciones para hablar muy bien de la plataforma y no hacer spam.
