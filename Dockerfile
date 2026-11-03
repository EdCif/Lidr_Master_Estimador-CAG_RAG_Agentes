# Dockerfile · Receta para construir la imagen de la API (sesión 7).
#
# Una imagen es una «caja» cerrada con el programa y todo lo que necesita, que
# funciona igual en cualquier ordenador. Esta receta tiene dos etapas:
#
#   1. «builder»: instala las librerías del proyecto (con uv, la misma versión
#      que usa la CI) en un entorno virtual.
#   2. «runtime»: parte de una imagen de Python limpia y copia solo el entorno
#      ya instalado y nuestro código. Así la imagen final no lleva uv ni los
#      restos de la instalación.
#
# La clave de OpenAI NO entra en la imagen: se inyecta al arrancar el
# contenedor (ver docker-compose.yml y .dockerignore). Una imagen se puede
# compartir; la clave es tuya.

# ---------------------------------------------------------------------------
# Etapa 1 · builder: instalar las dependencias
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

# uv (el instalador de librerías) se copia desde su imagen oficial. La versión
# está fijada (la misma que .github/workflows/validate.yml) para que la imagen
# sea reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /bin/

WORKDIR /app

# UV_COMPILE_BYTECODE: deja las librerías precompiladas (arranca más rápido).
# UV_LINK_MODE=copy: copia los ficheros en vez de enlazarlos (dentro de Docker
# no hay caché compartida con el sistema, y los enlaces darían avisos).
# UV_PYTHON_DOWNLOADS=never: usar el Python de la imagen, no bajar otro.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Primero SOLO los dos ficheros que describen las librerías. Docker guarda cada
# paso en una «capa» y la reutiliza si lo que entra no ha cambiado: mientras no
# toques pyproject.toml ni uv.lock, reconstruir la imagen no vuelve a instalar
# nada (solo cambia la capa de nuestro código, más abajo).
COPY pyproject.toml uv.lock ./

# --locked: usa exactamente las versiones de uv.lock (si no cuadran, falla).
# --no-dev: sin las librerías de pruebas (pytest...).
# --no-install-project: el proyecto no es un paquete instalable; solo sus librerías.
RUN uv sync --locked --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Etapa 2 · runtime: la imagen final
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Un usuario sin privilegios: si alguien consiguiera ejecutar algo dentro del
# contenedor, no sería «administrador». (La API no necesita serlo.)
RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

# El entorno ya instalado (la ruta es la misma que en la etapa 1, por eso sus
# enlaces al Python de la imagen siguen valiendo).
COPY --from=builder /app/.venv /app/.venv

# Nuestro código. Solo lo necesario: lo demás (tests, planes, cuadernos, el
# .env...) lo deja fuera .dockerignore.
COPY app ./app
COPY scripts ./scripts
COPY docs ./docs

# La carpeta de datos (la SQLite de la sesión 1, los vectores generados...).
# En docker-compose se sustituye por la carpeta data/ de tu ordenador.
RUN mkdir -p /app/data && chown -R app:app /app/data

# Poner el entorno virtual el primero en el PATH: «python» y «uvicorn» son los
# del proyecto. PYTHONUNBUFFERED hace que los logs salgan al momento (sin
# esperar a llenar un búfer), y PYTHONDONTWRITEBYTECODE evita ensuciar el disco.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

# Dentro del contenedor la API escucha en el 8000; docker-compose lo publica en
# el 8001 de tu ordenador. Es solo documentación: EXPOSE no abre nada por sí solo.
EXPOSE 8000

# Cada 30 s pregunta a la propia API si está bien (/health). Con esto,
# «docker compose ps» dice «healthy» o «unhealthy». La imagen slim no trae curl,
# así que se usa Python. start-period da margen para arrancar (carga muchas
# librerías).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# 0.0.0.0 = escuchar en todas las direcciones DEL CONTENEDOR (hace falta para
# que el puerto publicado llegue a la API). Quién puede entrar desde fuera lo
# decide docker-compose.yml (solo desde tu ordenador).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
