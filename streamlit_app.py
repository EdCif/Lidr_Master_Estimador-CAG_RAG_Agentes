"""Estimador CAG · interfaz conversacional con Streamlit (sesión 2).

Arranque, desde la carpeta ``estimador-cag``:

    uv run streamlit run streamlit_app.py

Streamlit abre http://localhost:8501. La app FastAPI de la sesión 1 sigue
disponible de forma independiente en el puerto 8001.

Cómo funciona Streamlit (la idea clave para entender este fichero)
------------------------------------------------------------------
Streamlit ejecuta ESTE SCRIPT COMPLETO, de arriba abajo, cada vez que el
usuario interactúa (escribe un mensaje, pulsa un botón...). No hay callbacks
ni bucle de eventos que programar: cada interacción es una nueva ejecución
del script. Por eso:

* Todo lo que deba sobrevivir entre ejecuciones (el historial del chat, las
  métricas de la última llamada) se guarda en ``st.session_state``, un
  diccionario que Streamlit conserva mientras la pestaña del navegador siga
  abierta.
* El historial se vuelve a dibujar en cada ejecución recorriendo esa lista.

El fichero está dividido en secciones numeradas. Tras este paso la respuesta
llega en streaming (token a token) y cada petición lleva el contexto CAG de
la sesión 1 como mensaje "system". El panel lateral se añade en el paso
siguiente de la sesión.
"""

# ---------------------------------------------------------------------------
# 0. Imports y constantes
# ---------------------------------------------------------------------------
import time

import litellm
import streamlit as st

# Reutilizamos la configuración centralizada de la sesión 1: lee .env y el
# entorno y expone las claves como SecretStr (no se muestran por accidente).
from app.config import get_settings

# Reutilizamos también el system prompt de la sesión 1: instrucciones de
# estimación + ejemplos de referencia (contexto CAG). Es la misma función que
# usa la app FastAPI, así que las dos interfaces estiman con idénticas reglas.
from app.services.llm_service import build_system_prompt

# Anclaje al modelo. litellm usa el formato "<proveedor>/<modelo>": el prefijo
# "anthropic/" le indica que hable con la API de Anthropic. Cambiar de modelo
# es cambiar esta única línea.
MODEL = "anthropic/claude-sonnet-5"

# Tope de tokens que puede generar el modelo en cada respuesta. Una estimación
# completa ocupa entre 1.500 y 3.000 tokens; 8.000 deja margen sin permitir
# respuestas desmesuradas.
MAX_TOKENS = 8000

# ---------------------------------------------------------------------------
# 1. Configuración de la página
# ---------------------------------------------------------------------------
# set_page_config debe ser la PRIMERA orden de Streamlit del script. Define el
# título de la pestaña del navegador, el icono y el ancho de la página.
st.set_page_config(
    page_title="Estimador CAG · Streamlit",
    page_icon="📐",
    layout="wide",
)
st.title("Estimador CAG · interfaz conversacional")
# st.caption(f"Modelo anclado: `{MODEL}`")
# Cambiar el widget st.markdown pinta a tamaño normal de párrafo y st.subheader a tamaño de subtítulo
#st.markdown(f"**Modelo anclado:** `{MODEL}`")
st.subheader(f"Modelo anclado: {MODEL}")


# Estilo de la caja de chat. Streamlit no expone el tamaño de letra del
# placeholder como parámetro, así que se ajusta por CSS. El selector usa el
# data-testid del componente (puede cambiar entre versiones de Streamlit).
st.markdown(
    """
    <style>
    /* Texto que escribe el usuario dentro de la caja */
    [data-testid="stChatInputTextArea"] { font-size: 1.15rem; }
    /* Texto de ayuda que aparece cuando la caja está vacía */
    [data-testid="stChatInputTextArea"]::placeholder { font-size: 1.15rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# 2. Estado de sesión
# ---------------------------------------------------------------------------
# st.session_state se comporta como un diccionario. La primera vez que se
# ejecuta el script no contiene nada, así que inicializamos las claves que
# vamos a usar. En las ejecuciones siguientes ya existen y se conservan.
#
# "messages" es el historial del chat, en el mismo formato que espera la API
# (lista de {"role": "user" | "assistant", "content": texto}). Así la misma
# lista sirve para pintar la conversación y para enviarla al modelo.
if "messages" not in st.session_state:
    st.session_state.messages = []

# Métricas de la última llamada (modelo, tokens...). Se rellenan en la
# sección 3 y se mostrarán en el panel lateral en un paso posterior.
if "last_metrics" not in st.session_state:
    st.session_state.last_metrics = None


# ---------------------------------------------------------------------------
# 3. Contexto CAG y llamada al modelo
# ---------------------------------------------------------------------------
# Contexto CAG: el conocimiento de referencia (instrucciones de estimación y
# ejemplos ficticios de app/context/examples.py) se incorpora a CADA petición
# como mensaje "system". No se busca ni se recupera nada en tiempo de
# ejecución: el contexto vive en el repositorio y viaja siempre con el prompt.
#
# ANTES (pasos 1 y 2, sin contexto CAG): no había system prompt. El modelo
# recibía solo la transcripción y respondía como un asistente genérico.
# SYSTEM_PROMPT = None
#
# AHORA (paso 3, con contexto CAG): el system prompt se construye una vez por
# ejecución del script. Es solo texto; no llama a ninguna API.
SYSTEM_PROMPT = build_system_prompt()


def get_anthropic_api_key() -> str:
    """Devuelve la clave de Anthropic o detiene la app con un aviso claro."""
    settings = get_settings()
    if settings.anthropic_api_key is None:
        st.error(
            "Falta ANTHROPIC_API_KEY en el archivo .env. Añádela y reinicia "
            "la aplicación (Ctrl+C y vuelve a lanzar `uv run streamlit run`)."
        )
        # st.stop() interrumpe la ejecución del script en este punto; lo que
        # ya se ha dibujado (título, historial) se mantiene en pantalla.
        st.stop()
    return settings.anthropic_api_key.get_secret_value()


def stream_model(history: list[dict[str, str]]):
    """Envía el historial completo al modelo y entrega la respuesta a trozos.

    Es un GENERADOR: en lugar de devolver el texto completo con ``return``,
    hace ``yield`` de cada fragmento en cuanto llega del modelo. Quien lo
    consume (st.write_stream, en la sección 5) lo pinta al instante, y así la
    respuesta "se escribe" en pantalla token a token.

    La API de los modelos es "sin estado": no recuerda llamadas anteriores.
    Por eso enviamos en cada petición toda la conversación acumulada en
    session_state; así el modelo puede atender peticiones como "reduce el
    alcance" sabiendo qué estimó antes.

    Las métricas (modelo, tokens, tiempos) no pueden salir por ``return`` en
    un generador, así que se guardan en st.session_state.last_metrics cuando
    llega el último chunk.
    """
    started = time.perf_counter()  # reloj de alta precisión para medir tiempos
    first_token_at: float | None = None

    response = litellm.completion(
        model=MODEL,
        # ANTES (pasos 1 y 2, sin contexto CAG): solo el historial visible.
        # messages=history,
        #
        # AHORA (paso 3, con contexto CAG): el mensaje "system" va SIEMPRE el
        # primero y delante del historial. No se guarda en session_state
        # porque no es parte de la conversación visible: es el "manual de
        # instrucciones" que acompaña a cada petición.
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
        api_key=get_anthropic_api_key(),
        max_tokens=MAX_TOKENS,
        # stream=True cambia lo que devuelve completion(): en vez de una
        # respuesta completa, un iterador de "chunks" que van llegando según
        # el modelo los genera.
        stream= True,
        # Pide que el ÚLTIMO chunk incluya el consumo de tokens (usage). Sin
        # esto, en streaming no sabríamos cuántos tokens se han utilizado.
        stream_options={"include_usage": True},
    )

    # litellm normaliza los chunks de cualquier proveedor al mismo formato:
    # el texto viene en choices[0].delta.content y el consumo en usage.
    for chunk in response:
        # Chunks de texto: traen unas pocas letras en delta.content. En los
        # chunks de control ese campo es None, por eso se comprueba.
        if chunk.choices and chunk.choices[0].delta.content:
            if first_token_at is None:
                first_token_at = time.perf_counter()
            yield chunk.choices[0].delta.content

        # Último chunk: no trae texto (choices vacío) pero sí usage.
        usage = getattr(chunk, "usage", None)
        if usage:
            finished = time.perf_counter()
            st.session_state.last_metrics = {
                "model": chunk.model,
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                # Tiempo hasta el primer token: lo que "espera" el usuario.
                "first_token_s": (first_token_at or finished) - started,
                # Tiempo total de la respuesta.
                "total_s": finished - started,
            }


def format_int(value: int) -> str:
    """Formatea un entero con punto de millares (2445 -> 2.445)."""
    return f"{value:,}".replace(",", ".")


def render_metrics(metrics: dict[str, object] | None) -> None:
    """Dibuja las métricas de la última llamada en columnas legibles.

    st.columns reparte el ancho disponible en columnas (la lista indica el
    peso relativo de cada una: la del modelo es el doble de ancha porque su
    texto es más largo). st.metric muestra dentro de cada columna un rótulo
    pequeño y el valor en grande. Esta función se reutilizará en el panel
    lateral en el siguiente paso.
    """
    if not metrics:
        st.caption("Sin llamadas todavía.")
        return

    col_model, col_in, col_out, col_first, col_total = st.columns([2, 1, 1, 1, 1])
    col_model.metric("Modelo", metrics["model"])
    col_in.metric("Tokens entrada", format_int(metrics["input_tokens"]))
    col_out.metric("Tokens salida", format_int(metrics["output_tokens"]))
    col_first.metric("Primer token", f"{metrics['first_token_s']:.1f} s")
    col_total.metric("Tiempo total", f"{metrics['total_s']:.1f} s")


# ---------------------------------------------------------------------------
# 4. Historial visible
# ---------------------------------------------------------------------------
# Como el script se re-ejecuta entero en cada interacción, el historial se
# vuelve a pintar cada vez a partir de session_state. st.chat_message crea la
# "burbuja" con el avatar del rol; dentro pintamos el texto como Markdown.
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# ---------------------------------------------------------------------------
# 5. Entrada del usuario y respuesta del modelo
# ---------------------------------------------------------------------------
# st.chat_input dibuja la caja de texto de altura 160, fija en la parte inferior. Devuelve
# None en todas las ejecuciones salvo en la que el usuario acaba de enviar un
# mensaje; en esa devuelve el texto escrito.
prompt = st.chat_input("Pega aquí la petición o la transcripción de la reunión", height=160)

if prompt:
    # 5.1 Guardamos y pintamos el mensaje del usuario. Se guarda ANTES de
    #     llamar al modelo para que forme parte del historial enviado.
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # 5.2 Llamamos al modelo dentro de la burbuja del asistente.
    #     st.write_stream consume el generador: pinta cada fragmento en cuanto
    #     llega (efecto "escribiendo") y, al terminar, devuelve el texto
    #     completo ya concatenado, que es lo que guardamos en el historial.
    with st.chat_message("assistant"):
        answer = st.write_stream(stream_model(st.session_state.messages))

    # 5.3 Guardamos la respuesta para las próximas ejecuciones. Las métricas ya
    #     las dejó el generador en st.session_state.last_metrics.
    st.session_state.messages.append({"role": "assistant", "content": answer})

    # 5.4 Métricas de la llamada, en columnas bajo la respuesta.
    #     ANTES: una línea de texto pequeño con st.caption.
    #     AHORA: columnas con st.metric (ver render_metrics en la sección 3).
    #     En el paso del panel lateral se mostrarán también allí.
    st.divider()
    render_metrics(st.session_state.last_metrics)
