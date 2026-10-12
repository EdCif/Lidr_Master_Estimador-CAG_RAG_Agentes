"""Estimador CAG · interfaz Streamlit de la sesión 5: memoria vs historial.

Arranque, desde la carpeta ``estimador-cag``, en DOS terminales:

    uv run uvicorn app.main:app --host 127.0.0.1 --port 8001   # 1) la API
    uv run streamlit run streamlit_app.py                       # 2) esta interfaz

Streamlit abre http://localhost:8501 y habla con la API de sesiones del
puerto 8001 (se puede cambiar con API_BASE_URL en el .env).

Qué cambia respecto a la sesión 2
---------------------------------
ANTES (sesión 2): Streamlit llamaba al modelo directamente con litellm y
guardaba TODO el historial en el navegador. Cada petición mandaba la
conversación entera, sin memoria ni adjuntos.

AHORA (sesión 5): Streamlit es solo la "cara" de la aplicación. Todo lo
importante lo hace la API de sesiones (``app/routers/sessions.py``):

* Al abrir la página se crea una sesión (``POST /sessions``) y su
  ``session_id`` se guarda en ``st.session_state``.
* Cada turno se envía a ``POST /sessions/{id}/estimate`` con la
  transcripción y los adjuntos. La respuesta llega en streaming (SSE) y se
  pinta según llega.
* El panel lateral enseña, por separado, la MEMORIA (``project_metadata``,
  la ficha con los hechos del proyecto) y el HISTORIAL (la ventana deslizante
  de turnos), que es justo la idea de la sesión 5.
* Si la sesión caduca por inactividad (410), se ofrece empezar de cero o
  heredar la memoria.

Cómo funciona Streamlit (la idea clave para entender este fichero)
------------------------------------------------------------------
Streamlit ejecuta ESTE SCRIPT COMPLETO, de arriba abajo, cada vez que el
usuario interactúa (pulsa un botón, envía el formulario...). Lo que tenga que
sobrevivir entre ejecuciones se guarda en ``st.session_state``, un
diccionario que Streamlit conserva mientras la pestaña siga abierta.
"""

# ---------------------------------------------------------------------------
# 0. Imports y constantes
# ---------------------------------------------------------------------------
import json
import re
import time
from datetime import datetime

import httpx
import streamlit as st

from app.config import get_settings
from app.services.prompt_builder import build_session_system_prompt
from app.services.sessions import ProjectMetadata

SETTINGS = get_settings()

# Dónde está la API de sesiones. En local, la que arranca uvicorn en el 8001.
API_BASE_URL = SETTINGS.api_base_url.rstrip("/")

# Tiempos de espera de las llamadas a la API. Conectar debe ser inmediato;
# leer puede tardar: Opus razona antes de escribir y la API manda pings cada
# pocos segundos para que la conexión no se corte mientras tanto.
TIMEOUT = httpx.Timeout(10.0, read=300.0)

# Etiquetas de los dos caminos de adjuntos, tal como se muestran al usuario.
PATHS = {
    "local": "B · Extracción local (pypdf / MarkItDown / python-docx)",
    "files_api": "A · Files API de Anthropic (Claude lee el PDF entero)",
}

# Versión corta, para el selector del formulario (cabe en una línea y así el
# formulario entra entero en pantalla). La larga sale como ayuda al pasar el ratón.
PATHS_SHORT = {"local": "B · Local", "files_api": "A · Files API"}

# Nombres legibles de cada dato de la memoria.
FACT_LABELS = {
    "project_name": "Proyecto",
    "assumed_team_size": "Equipo asumido",
    "mentioned_technologies": "Tecnologías",
    "agreed_scope": "Alcance acordado",
}


# ---------------------------------------------------------------------------
# 1. Configuración de la página
# ---------------------------------------------------------------------------
# set_page_config debe ser la PRIMERA orden de Streamlit del script.
st.set_page_config(page_title="Estimador · Memoria vs Historial", page_icon="🧠", layout="wide")
st.title("🧠 Estimador CAG · memoria vs historial")
st.caption(
    f"Sesión 5 · Conversación de varios turnos contra la API `{API_BASE_URL}` · "
    f"modelo `{SETTINGS.session_llm_model}`"
)

# Estilos. Streamlit no deja elegir estas cosas con parámetros, así que van
# por CSS. Los selectores usan los data-testid de Streamlit y la clase
# "st-key-workspace" que pone st.container(key="workspace") en la sección 5
# (pueden cambiar entre versiones de Streamlit).
st.markdown(
    """
    <style>
    /* 0) Colores y letra principal (Palatino) vienen del tema, en
          .streamlit/config.toml. Aquí, las etiquetas, los textos pequeños y
          los botones pasan a Gill Sans, la letra "de apoyo" de la paleta. */
    /* Streamlit pone la letra en el párrafo de DENTRO de cada etiqueta, así
       que la regla se aplica también a los hijos. Los iconos se excluyen:
       usan su propia fuente de iconos y se romperían. */
    :is([data-testid="stWidgetLabel"], [data-testid="stCaptionContainer"],
        [data-testid="stMetricLabel"], [data-testid="stExpander"] summary,
        [data-testid^="stBaseButton"], [data-testid="stFileUploaderDropzone"],
        [data-testid="stRadio"] label),
    :is([data-testid="stWidgetLabel"], [data-testid="stCaptionContainer"],
        [data-testid="stMetricLabel"], [data-testid="stExpander"] summary,
        [data-testid^="stBaseButton"], [data-testid="stFileUploaderDropzone"],
        [data-testid="stRadio"] label) *:not([data-testid="stIconMaterial"]) {
        font-family: "Gill Sans MT", "Gill Sans", "Trebuchet MS", "Segoe UI", Arial, sans-serif !important;
    }
    /* El ID de la sesión, en el panel lateral, algo más grande y legible. */
    [data-testid="stSidebar"] [data-testid="stCode"] code {
        font-size: 0.95rem !important;
        word-break: break-all;  /* si el panel es estrecho, salta de línea en vez de cortarse */
    }
    /* Línea dorada fina bajo el título de la página, como acento. */
    [data-testid="stHeading"] h1 {
        border-bottom: 2px solid #C8A96E;
        padding-bottom: 0.4rem;
    }

    /* 1) La misma letra y el mismo tamaño en preguntas y respuestas. Claude
          escribe títulos (#, ##) que Streamlit pintaría en grande: aquí
          quedan en negrita pero del tamaño del resto del texto. */
    [data-testid="stChatMessage"] :is(p, li, td, th, blockquote, h1, h2, h3, h4, h5, h6) {
        font-family: inherit !important;
        font-size: 1rem !important;
        line-height: 1.6 !important;
    }
    [data-testid="stChatMessage"] :is(h1, h2, h3, h4, h5, h6) {
        font-weight: 700 !important;
        margin: 0.9rem 0 0.3rem 0 !important;
        padding: 0 !important;
    }

    /* 2) La columna de la transcripción se queda fija en pantalla mientras
          se baja por la conversación (position: sticky). */
    .st-key-workspace [data-testid="stHorizontalBlock"] { align-items: flex-start; }
    .st-key-workspace [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child {
        position: sticky;
        top: 4.5rem;  /* justo por debajo de la barra superior de Streamlit */
        z-index: 2;
    }
    /* Streamlit deja ~160 px vacíos al final de la página. Un elemento
       "sticky" no puede salir de su contenedor, así que con ese hueco el
       formulario subiría al llegar abajo del todo en pantallas bajas. */
    [data-testid="stMainBlockContainer"] { padding-bottom: 1.5rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# 2. Hablar con la API de sesiones
# ---------------------------------------------------------------------------
class ApiError(Exception):
    """La API ha respondido con un error (404, 410, 413...). Lleva el código y el detalle."""

    def __init__(self, status_code: int, detail: object) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _detail(response: httpx.Response) -> object:
    """El campo "detail" de un error de FastAPI, o el texto tal cual."""
    try:
        return response.json().get("detail", response.text)
    except ValueError:
        return response.text


def api_create_session(inherit_from: str | None = None) -> dict:
    """POST /sessions: sesión nueva, de cero o heredando la memoria de otra."""
    body = {"inherit_from": inherit_from} if inherit_from else None
    response = httpx.post(f"{API_BASE_URL}/sessions", json=body, timeout=TIMEOUT)
    if response.status_code != 201:
        raise ApiError(response.status_code, _detail(response))
    return response.json()


def api_get_session(session_id: str) -> dict:
    """GET /sessions/{id}: la memoria y el historial actuales, por separado."""
    response = httpx.get(f"{API_BASE_URL}/sessions/{session_id}", timeout=TIMEOUT)
    if response.status_code != 200:
        raise ApiError(response.status_code, _detail(response))
    return response.json()


def iter_sse(response: httpx.Response):
    """Lee la respuesta en streaming y devuelve (evento, datos) según llegan.

    El formato SSE es texto, línea a línea: "event: delta", "data: {...}" y
    una línea en blanco para cerrar cada evento. Las líneas que empiezan por
    ":" son comentarios (los pings que manda FastAPI para mantener viva la
    conexión) y se ignoran.
    """
    event, data_lines = None, []
    for line in response.iter_lines():
        if line == "":
            if data_lines:
                try:
                    yield event or "message", json.loads("\n".join(data_lines))
                except ValueError:
                    pass  # un evento sin JSON (un ping): nada que hacer
            event, data_lines = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())


def stream_turn(session_id: str, transcript: str, uploads: list, mode: str, status_box):
    """Envía un turno a la API y entrega la estimación a trozos.

    Es un GENERADOR: cada trozo de texto ("delta") sale con ``yield`` en
    cuanto llega, y st.write_stream lo va pintando. Los mensajes de estado
    ("Extrayendo texto...", "Actualizando la memoria...") van a la caja
    ``status_box``. El resultado final (evento "done" o "error") y los
    tiempos se dejan en ``st.session_state.turn_result``.
    """
    result = st.session_state.turn_result = {"done": None, "error": None, "first_token_s": None}
    started = time.perf_counter()
    files = [("attachments", (f.name, f.getvalue(), f.type or "application/octet-stream")) for f in uploads]

    with httpx.Client(timeout=TIMEOUT) as client:
        with client.stream(
            "POST",
            f"{API_BASE_URL}/sessions/{session_id}/estimate",
            data={"transcript": transcript, "attachment_mode": mode},
            files=files or None,
        ) as response:
            # Los errores de antes del streaming (sesión caducada, fichero
            # demasiado grande...) llegan con su código HTTP de verdad.
            if response.status_code != 200:
                response.read()
                raise ApiError(response.status_code, _detail(response))
            for event, data in iter_sse(response):
                if event == "status":
                    status_box.write(f"· {data['message']}")
                elif event == "delta":
                    if result["first_token_s"] is None:
                        result["first_token_s"] = time.perf_counter() - started
                    yield data["text"]
                elif event == "done":
                    result["done"] = data
                elif event == "error":
                    result["error"] = data["detail"]
    result["total_s"] = time.perf_counter() - started


def format_int(value: int) -> str:
    """Formatea un entero con punto de millares (2445 -> 2.445)."""
    return f"{value:,}".replace(",", ".")


_FACTS_BLOCK = re.compile(r"<hechos_del_proyecto>(.*?)</hechos_del_proyecto>", re.S | re.I)


def pretty_answer(text: str) -> str:
    """Muestra el bloque <hechos_del_proyecto> como una lista limpia.

    El modelo cierra cada respuesta con ese bloque (lo lee el extractor de
    memoria del Paso 01.3). Pintado tal cual, en Markdown sale como una línea
    con etiquetas raras; aquí lo convertimos en una lista con su título.
    """

    def as_list(match: re.Match[str]) -> str:
        lines = [line.strip() for line in match.group(1).splitlines() if line.strip()]
        items = "\n".join(f"- {line}" for line in lines)
        return f"\n\n---\n**📌 Hechos del proyecto detectados en este turno**\n\n{items}\n"

    return _FACTS_BLOCK.sub(as_list, text)


def local_time(iso_text: str | None) -> str:
    """Convierte una hora ISO de la API (UTC) a la hora local, "HH:MM"."""
    if not iso_text:
        return "—"
    return datetime.fromisoformat(iso_text.replace("Z", "+00:00")).astimezone().strftime("%H:%M")


# ---------------------------------------------------------------------------
# 3. Estado de la página
# ---------------------------------------------------------------------------
# ANTES (sesión 2): "messages" era el historial que se mandaba al modelo.
# AHORA: el historial de verdad lo guarda la API. Aquí solo guardamos lo que
# hace falta para PINTAR la conversación (todos los turnos, también los que ya
# salieron de la ventana) y el session_id con el que hablamos con la API.
defaults = {
    "session_id": None,  # a) se crea al cargar la página
    "turns": [],  # turnos para pintar: {"user", "attachments", "assistant", "mode"}
    "metrics": None,  # métricas de la última llamada
    "turn_result": None,  # lo que deja stream_turn (done / error / tiempos)
    "expired": None,  # detalle del 410 si la sesión ha caducado
    "changed_facts": set(),  # datos de la memoria que cambiaron en el último turno
    "inherited_notice": None,  # aviso de "memoria heredada de..."
}
for key, value in defaults.items():
    st.session_state.setdefault(key, value)


def start_new_session(inherit_from: str | None = None) -> None:
    """d) Nueva conversación: pide otra sesión a la API y limpia la pantalla."""
    created = api_create_session(inherit_from)
    st.session_state.session_id = created["session_id"]
    st.session_state.turns = []
    st.session_state.metrics = None
    st.session_state.expired = None
    st.session_state.changed_facts = set()
    st.session_state.inherited_notice = inherit_from


# a) Crear la sesión al cargar la página. Si la API no está arrancada, lo
#    decimos claramente y paramos aquí.
try:
    if st.session_state.session_id is None:
        start_new_session()
    session_state = api_get_session(st.session_state.session_id)
except httpx.ConnectError:
    st.error(
        f"No hay ninguna API escuchando en {API_BASE_URL}. Arráncala en otra terminal, "
        "desde la carpeta estimador-cag:\n\n"
        "`uv run uvicorn app.main:app --host 127.0.0.1 --port 8001`\n\n"
        "y recarga esta página."
    )
    st.stop()
except ApiError as error:
    session_state = None
    if error.status_code == 410:
        # La sesión caducó por inactividad: guardamos su detalle para
        # ofrecer las dos opciones más abajo.
        st.session_state.expired = error.detail
    elif error.status_code == 404:
        # La API se ha reiniciado y la sesión (que vive en su memoria) se ha
        # perdido. Creamos otra sin más.
        start_new_session()
        st.toast("La API se ha reiniciado y la sesión anterior se perdió: empezamos una nueva.", icon="🔄")
        st.rerun()
    else:
        st.error(f"La API ha respondido {error.status_code}: {error.detail}")
        st.stop()


# ---------------------------------------------------------------------------
# 4. Sesión caducada (TTL): empezar de cero o heredar la memoria
# ---------------------------------------------------------------------------
if st.session_state.expired:
    expired = st.session_state.expired
    st.warning(f"⏰ {expired['message']}")
    facts = {k: v for k, v in expired["project_metadata"].items() if v}
    if facts:
        st.markdown("**Memoria que se puede heredar:**")
        st.json(facts)
    left, right = st.columns(2)
    if left.button("🆕 Empezar de cero", width="stretch"):
        start_new_session()
        st.rerun()
    if right.button("🧠 Continuar con la memoria heredada", type="primary", width="stretch"):
        start_new_session(inherit_from=expired["session_id"])
        st.rerun()
    st.stop()

# ---------------------------------------------------------------------------
# 5. Disposición de la pantalla: transcripción a la izquierda, conversación a
#    la derecha
# ---------------------------------------------------------------------------
# ANTES: el formulario iba debajo del chat, y cada respuesta larga lo empujaba
# hacia abajo. AHORA: dos columnas. La de la izquierda (transcripción y
# documentos) se queda fija en pantalla gracias al CSS "sticky" de la
# sección 1; la de la derecha crece con la conversación.
with st.container(key="workspace"):
    input_col, chat_col = st.columns([2, 3], gap="large")

# 5.1 Cabecera de la conversación y dos huecos, en este orden:
#     * live_turn: ARRIBA del todo, para el turno que se está generando.
#     * previous_turns: debajo, los turnos ya terminados, del más reciente al
#       más antiguo.
#     Los huecos se crean ahora y se rellenan más abajo (secciones 7 y 8):
#     Streamlit pinta cada cosa en su hueco, aunque el código vaya después.
with chat_col:
    st.markdown("##### 💬 Conversación")
    if st.session_state.inherited_notice:
        st.info(
            f"🧠 Esta sesión ha heredado la memoria de la sesión `{st.session_state.inherited_notice[:8]}…`. "
            "El historial empieza vacío, pero el modelo ya conoce los hechos del proyecto."
        )
    live_turn = st.container()
    previous_turns = st.container()


def turn_title(number: int, turn: dict, outside_window: bool) -> str:
    """Título del desplegable de un turno: número, inicio del mensaje y adjuntos."""
    text = " ".join(turn["user"].split())
    preview = text if len(text) <= 70 else text[:69].rstrip() + "…"
    title = f"Turno {number} · «{preview}»"
    if turn["attachments"]:
        title += f" · 📎 {len(turn['attachments'])}"
    if outside_window:
        title += " · 🕳️ fuera de la ventana"
    return title


# ---------------------------------------------------------------------------
# 6. Formulario del turno (b: transcripción + selector múltiple de ficheros)
# ---------------------------------------------------------------------------
# st.form agrupa los campos: nada se envía hasta pulsar "Estimar", y
# clear_on_submit deja el formulario vacío para el siguiente turno.
first_turn = not st.session_state.turns
with input_col:
    st.markdown("##### 📝 Transcripción y documentos")
    with st.form("turn_form", clear_on_submit=True):
        transcript = st.text_area(
            "Transcripción de la reunión" if first_turn else "Siguiente mensaje (cambios, dudas, nuevos datos...)",
            height=150,
            placeholder=(
                "Ej.: «El proyecto se llama Portal de Reservas. Necesitamos una web para reservar salas... "
                "Las tecnologías son React y FastAPI. Suponed un equipo de 3 personas.»"
                if first_turn
                else "Ej.: «Cambio de planes: el equipo será de 4 personas y quitad la app móvil.»"
            ),
        )
        uploads = st.file_uploader(
            "Documentación complementaria (opcional): PDF o Word",
            type=["pdf", "docx"],
            accept_multiple_files=True,
        )
        mode = st.radio(
            "Conversión y tratamiento de adjuntos",
            options=list(PATHS),
            format_func=PATHS_SHORT.get,
            index=list(PATHS).index(SETTINGS.attachment_mode),
            horizontal=True,
            help=f"{PATHS['local']}. {PATHS['files_api']}.",
        )
        submitted = st.form_submit_button("📐 Estimar", type="primary", width="stretch")
    if submitted and not transcript.strip():
        st.warning("Escribe la transcripción o el mensaje antes de pulsar «Estimar».")

generating = bool(submitted and transcript.strip())


# ---------------------------------------------------------------------------
# 7. Turnos anteriores: el más reciente arriba, cada uno en un desplegable
# ---------------------------------------------------------------------------
# Pintamos TODOS los turnos, aunque el modelo ya no los vea: los que han salido
# de la ventana deslizante lo dicen en su título. Es la diferencia entre
# historial (se recorta) y memoria (los hechos de esos turnos siguen en la
# ficha). El último turno sale abierto; los demás, cerrados. Mientras se
# genera uno nuevo, todos los anteriores se cierran.
with previous_turns:
    turns = st.session_state.turns
    if not turns and not generating:
        st.caption("Aquí aparecerá la conversación. Escribe la transcripción a la izquierda y pulsa «Estimar».")
    discarded = session_state["discarded_turns"] if session_state else 0
    for index in reversed(range(len(turns))):
        turn = turns[index]
        outside_window = index < discarded
        is_latest = index == len(turns) - 1
        title = turn_title(index + 1, turn, outside_window)
        # El turno más reciente va en una tarjeta SIEMPRE abierta; los
        # anteriores, en desplegables cerrados. (Si el último también fuera un
        # desplegable, Streamlit recordaría que en esa posición había uno
        # cerrado mientras se generaba, y aparecería cerrado al terminar.)
        if is_latest and not generating:
            box = st.container(border=True)
            box.markdown(f"**{title}**")
        else:
            box = st.expander(title, expanded=False)
        with box:
            if outside_window:
                st.caption(
                    "🕳️ Fuera de la ventana: el modelo ya no ve este turno, "
                    "pero la memoria conserva sus hechos."
                )
            with st.chat_message("user"):
                st.markdown(turn["user"])
                if turn["attachments"]:
                    st.caption("📎 " + " · ".join(turn["attachments"]) + f" — {PATHS[turn['mode']]}")
            with st.chat_message("assistant"):
                st.markdown(pretty_answer(turn["assistant"]))


# ---------------------------------------------------------------------------
# 8. Enviar el turno y pintarlo en directo, arriba del todo (hueco live_turn)
# ---------------------------------------------------------------------------
if generating:
    names = [f.name for f in uploads]
    memory_before = session_state["project_metadata"] if session_state else {}
    live_card = live_turn.container(border=True)
    live_card.markdown(f"**Turno {len(st.session_state.turns) + 1} · en curso**")
    with live_card, st.chat_message("user"):
        st.markdown(transcript)
        if names:
            st.caption("📎 " + " · ".join(names) + f" — {PATHS[mode]}")

    with live_card, st.chat_message("assistant"):
        # La caja de estado se rellena con los eventos "status" de la API.
        status_box = st.status("Pensando la estimación…", expanded=False)
        try:
            answer = st.write_stream(
                stream_turn(st.session_state.session_id, transcript, uploads, mode, status_box)
            )
        except ApiError as error:
            status_box.update(label="No se ha podido completar el turno", state="error")
            if error.status_code == 410:
                st.session_state.expired = error.detail
                st.rerun()
            st.error(f"La API ha respondido {error.status_code}: {error.detail}")
            st.stop()
        except httpx.HTTPError as error:
            status_box.update(label="Se ha perdido la conexión con la API", state="error")
            st.error(f"Error de conexión con la API: {error.__class__.__name__}. Reintenta.")
            st.stop()

    result = st.session_state.turn_result
    if result["error"] or not result["done"]:
        # La API no guarda un turno que ha fallado: aquí tampoco lo pintamos.
        status_box.update(label="El turno ha fallado y no se ha guardado", state="error")
        live_card.error(result["error"] or "La respuesta se cortó antes de terminar.")
    else:
        done = result["done"]
        status_box.update(label=f"Turno completado en {result['total_s']:.0f} s", state="complete")
        st.session_state.turns.append(
            {"user": transcript, "attachments": names, "assistant": answer, "mode": mode}
        )
        st.session_state.metrics = {
            "model": done["model"],
            "mode": done["attachment_mode"],
            "input_tokens": done["usage"]["input_tokens"],
            "output_tokens": done["usage"]["output_tokens"],
            "first_token_s": result["first_token_s"] or result["total_s"],
            "total_s": result["total_s"],
        }
        # ¿Qué datos de la memoria ha cambiado este turno? Se marcan con 🆕.
        memory_after = done["project_metadata"]
        st.session_state.changed_facts = {
            key for key in FACT_LABELS if memory_after.get(key) != memory_before.get(key)
        }
        st.session_state.inherited_notice = None
        # Volvemos a ejecutar el script para que el chat y el panel lateral se
        # dibujen ya con el turno nuevo y la memoria actualizada.
        st.rerun()


# ---------------------------------------------------------------------------
# 9. Panel lateral (c: memoria y historial, por separado)
# ---------------------------------------------------------------------------
# Lo dibujamos al FINAL: así, tras un turno, ya refleja la memoria actualizada.
with st.sidebar:
    st.header("Panel de la sesión")
    state = session_state or {}
    # El identificador completo, en su propia caja (con botón para copiarlo,
    # útil para probar la sesión en /docs) y la caducidad en la línea de abajo.
    st.markdown("**ID de la sesión**")
    st.code(st.session_state.session_id, language=None, wrap_lines=True)
    st.caption(f"⏳ Caduca a las {local_time(state.get('expires_at'))} si no hay actividad")

    # c.1) LA MEMORIA: la ficha con los hechos del proyecto. Viaja dentro del
    #      system prompt de cada turno y no se recorta con la ventana.
    st.subheader("🧠 Memoria · project_metadata")
    memory = state.get("project_metadata", {})
    changed = st.session_state.changed_facts
    if not any(memory.values()):
        st.caption("Vacía: todavía no sabemos nada del proyecto. Se rellena sola después de cada turno.")
    else:
        for key, label in FACT_LABELS.items():
            value = memory.get(key)
            mark = " 🆕" if key in changed else ""
            if key == "mentioned_technologies":
                badges = " ".join(f":blue-badge[{tech}]" for tech in value) if value else "—"
                st.markdown(f"**{label}{mark}**  \n{badges}")
            elif key == "assumed_team_size":
                st.markdown(f"**{label}{mark}**  \n{f'{value} personas' if value else '—'}")
            else:
                st.markdown(f"**{label}{mark}**  \n{value or '—'}")

    # c.2) EL HISTORIAL: la ventana deslizante de turnos que viajan como
    #      mensajes. Cuando se llena, los turnos más antiguos se descartan.
    st.divider()
    st.subheader("📜 Historial · ventana deslizante")
    in_window = state.get("turn_count", 0)
    max_turns = state.get("max_turns", SETTINGS.max_turns)
    st.progress(
        min(in_window / max_turns, 1.0),
        text=f"{in_window} de {max_turns} turnos en la ventana",
    )
    if state.get("discarded_turns"):
        st.caption(
            f"🕳️ {state['discarded_turns']} turno(s) ya descartado(s): el modelo no los ve, "
            "pero sus hechos siguen en la memoria."
        )
    else:
        st.caption(
            "En cada llamada viajan el system prompt (con la memoria), los turnos de la "
            "ventana y la pregunta nueva."
        )

    # Lo que de verdad recibirá el modelo en el próximo turno: la plantilla
    # Jinja2 rellenada con la memoria actual (Paso 01.2).
    with st.expander("System prompt del próximo turno"):
        prompt = build_session_system_prompt(ProjectMetadata(**memory))
        st.caption(f"{format_int(len(prompt))} caracteres · plantilla app/prompts/estimator_system_v1.j2")
        st.text(prompt)

    # Métricas de la última llamada.
    st.divider()
    st.subheader("⏱️ Última llamada")
    metrics = st.session_state.metrics
    if not metrics:
        st.caption("Sin llamadas todavía.")
    else:
        left, right = st.columns(2)
        left.metric("Tokens entrada", format_int(metrics["input_tokens"]))
        right.metric("Tokens salida", format_int(metrics["output_tokens"]))
        left.metric("Primer token", f"{metrics['first_token_s']:.1f} s")
        right.metric("Tiempo total", f"{metrics['total_s']:.1f} s")
        st.caption(f"Modelo `{metrics['model']}` · camino {'A' if metrics['mode'] == 'files_api' else 'B'}")

    with st.expander("Ficha y estado en JSON (depuración)"):
        st.json(state, expanded=1)

    # d) Nueva conversación: pide otra sesión a la API y limpia la pantalla.
    st.divider()
    if st.button("Nueva conversación", type="primary", width="stretch"):
        start_new_session()
        st.rerun()
