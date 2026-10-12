"""Endpoints HTTP de las sesiones conversacionales (sesión 05).

Igual que el resto de routers, esta capa solo recibe la petición, delega y
devuelve. La lógica del historial y de la memoria está en
``app/services/sessions.py`` y la de cada turno en
``app/services/conversation_service.py``.

Las rutas cuelgan de /sessions, sin el prefijo /api/v1 que usan las de las
sesiones anteriores, porque así las define el enunciado de la sesión 05. Los
endpoints antiguos no se tocan y siguen funcionando igual.

Por qué casi todo se comprueba en dependencias (Depends)
--------------------------------------------------------
POST /sessions/{id}/estimate responde en streaming (Server-Sent Events). En
cuanto el generador del endpoint suelta su primer evento, la respuesta ya ha
salido con un 200 y no hay forma de cambiar ese código. Por eso todo lo que
puede fallar de antemano (que la sesión exista, que la transcripción no
esté vacía, que los adjuntos sean PDF o Word y no pesen demasiado, que haya
clave de Anthropic) se comprueba en dependencias. FastAPI las ejecuta
ANTES de abrir el stream, así que si algo falla el cliente recibe un 404,
413, 415, 422 o 503 de verdad. Lo que solo puede fallar durante el turno
(el proveedor cae, un PDF no tiene texto) llega como un evento ``error``.
"""

import asyncio
from collections.abc import AsyncIterable
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.sse import EventSourceResponse, ServerSentEvent

from app.config import get_settings
from app.schemas.session import SessionCreateRequest, SessionCreated, SessionState
from app.services.attachments.common import Attachment, AttachmentError, validate_attachment
from app.services.conversation_service import AttachmentMode, TurnSettings, stream_estimation_turn
from app.services.prompt_builder import build_session_system_prompt
from app.services.sessions import (
    Session,
    SessionExpiredError,
    SessionNotFoundError,
    SessionStore,
)


router = APIRouter(prefix="/sessions", tags=["sessions"])


# ---------------------------------------------------------------------------
# El almacén de sesiones (uno para todo el proceso)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_session_store() -> SessionStore:
    """Devuelve el almacén de sesiones del proceso, siempre el mismo objeto.

    lru_cache(maxsize=1) hace que el almacén se cree la primera vez que se
    pide y que las siguientes llamadas devuelvan ese mismo objeto. Es la misma
    técnica que usa get_settings() en config.py.

    Los endpoints lo reciben con Depends(get_session_store) en vez de
    importarlo directamente. Así, en los tests podemos sustituirlo por un
    almacén vacío con app.dependency_overrides y cada prueba empieza limpia.

    El tamaño de la ventana (MAX_TURNS) y el tope de sesiones (MAX_SESSIONS)
    se leen del .env. Como el almacén se crea una sola vez, un cambio en el
    .env necesita reiniciar la API.

    El system prompt lo monta build_session_system_prompt: la plantilla
    Jinja2 de app/prompts con la ficha del proyecto (project_metadata) en su
    bloque <project_metadata>. El almacén recibe la función, no el texto,
    para que cada sesión lo regenere con su ficha en cada turno.
    """
    settings = get_settings()
    return SessionStore(
        system_prompt_builder=build_session_system_prompt,
        max_turns=settings.max_turns,
        max_sessions=settings.max_sessions,
        ttl_seconds=settings.session_ttl_seconds,
    )


# ---------------------------------------------------------------------------
# Dependencias: comprobaciones que se hacen ANTES de abrir el stream
# ---------------------------------------------------------------------------

def get_existing_session(
    session_id: UUID,
    store: SessionStore = Depends(get_session_store),
) -> Session:
    """Busca la sesión de la URL. Si no se puede usar, responde 404 o 410.

    Declarar session_id como UUID hace que FastAPI rechace con un 422 los
    identificadores mal formados antes de llegar aquí. str(session_id) lo
    deja en el formato en que lo guardamos (minúsculas y con guiones), aunque
    el cliente lo mande en mayúsculas.

    * 404 (Not Found): no la conocemos (nunca existió, o el servidor se
      reinició y se perdió).
    * 410 (Gone, "ya no está"): existió, pero lleva más tiempo del TTL sin
      actividad y se ha archivado. La respuesta incluye su memoria y las dos
      opciones que tiene el cliente: empezar de cero o heredar esa memoria.
      Streamlit (Paso 04) usa esta información para mostrar dos botones.
    """
    try:
        return store.get(str(session_id))
    except SessionExpiredError as expired:
        archived = expired.archived
        minutes = int(store.ttl.total_seconds() // 60)
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "message": (
                    f"La sesión lleva más de {minutes} minutos sin actividad y se ha archivado. "
                    "Puedes empezar una sesión nueva de cero, o una nueva que herede su "
                    "memoria (los hechos del proyecto); el historial no se conserva."
                ),
                "session_id": archived.session_id,
                "archived_at": archived.archived_at.isoformat(),
                "project_metadata": archived.metadata.model_dump(),
                "options": {
                    "new": {"method": "POST", "path": "/sessions", "body": None},
                    "inherit": {
                        "method": "POST",
                        "path": "/sessions",
                        "body": {"inherit_from": archived.session_id},
                    },
                },
            },
        ) from None
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "La sesión no existe (puede que el servidor se haya reiniciado o que "
                "se superase el límite de sesiones). Crea una nueva con POST /sessions."
            ),
        ) from None


def read_transcript(
    transcript: Annotated[str, Form(
        description="Transcripción de la reunión, o el siguiente mensaje de la conversación.",
        max_length=200_000,
    )],
) -> str:
    """Lee el campo ``transcript`` del formulario y rechaza los vacíos."""
    text = transcript.strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "La transcripción no puede estar vacía.")
    return text


async def read_attachments(
    # Lista de ficheros, vacía por defecto (y no "lista o None"): con el
    # "o None", la página /docs no reconoce el campo como ficheros y manda su
    # contenido como texto. El formato "binary" es la forma clásica de decirle
    # a /docs que aquí va un selector de ficheros.
    #
    # Admitimos también texto (str) solo para un caso: /docs, con la casilla
    # "Send empty value" marcada, manda "attachments=" vacío cuando no eliges
    # ningún fichero. Eso significa "sin adjuntos" y lo ignoramos. Un texto
    # NO vacío en lugar de un fichero se rechaza más abajo con un 422 claro.
    attachments: Annotated[list[UploadFile | str], File(
        description="Documentación complementaria opcional: PDF o Word (.docx).",
        json_schema_extra={"items": {"type": "string", "format": "binary"}},
    )] = [],
    settings=Depends(get_settings),
) -> list[Attachment]:
    """Lee los ficheros adjuntos, comprueba cuántos son, su tamaño y su formato.

    También mira lo que costaría abrirlos (páginas de un PDF, tamaño
    descomprimido de un Word), para cortar ficheros trampa con un 413 antes
    de empezar el turno.

    Leemos los bytes aquí, al principio, y no dentro del stream: así los
    errores salen con su código HTTP y no dependemos de que el fichero
    temporal siga abierto mientras dura la respuesta.
    """
    # Texto en lugar de fichero: vacío = "sin adjuntos" (lo manda /docs);
    # con contenido = el cliente ha mandado el campo mal.
    if any(isinstance(item, str) and item.strip() for item in attachments):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "El campo attachments debe llevar ficheros (PDF o Word), no texto. "
            "En /docs, pulsa 'Add string item' y elige el fichero con 'Choose File'.",
        )
    # Algunos clientes mandan un "fichero" vacío y sin nombre cuando el
    # usuario no elige ninguno. Lo descartamos igual.
    # (Comprobamos "no es texto" y no "es UploadFile": los ficheros llegan
    # como el UploadFile de Starlette, la librería sobre la que va FastAPI.)
    files = [item for item in attachments if not isinstance(item, str) and item.filename]
    if len(files) > settings.max_attachments:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Se admiten como máximo {settings.max_attachments} adjuntos por petición.",
        )

    max_bytes = int(settings.max_attachment_mb * 1_048_576)
    max_docx_bytes = int(settings.max_docx_uncompressed_mb * 1_048_576)
    validated: list[Attachment] = []
    for file in files:
        # Leemos como mucho un byte más del límite: basta para saber si se
        # pasa, sin cargar en memoria un fichero de 2 GB que alguien suba por error.
        data = await file.read(max_bytes + 1)
        try:
            # Contar las páginas de un PDF o leer el índice de un Word es
            # rápido, pero es trabajo de CPU: va en otro hilo para no frenar
            # al resto de peticiones mientras tanto.
            validated.append(await asyncio.to_thread(
                validate_attachment,
                file.filename,
                data,
                max_bytes,
                settings.max_pdf_pages,
                max_docx_bytes,
            ))
        except AttachmentError as error:
            raise HTTPException(error.status_code, str(error)) from None
    return validated


def resolve_attachment_mode(
    attachment_mode: Annotated[AttachmentMode | None, Form(
        description="Camino de los adjuntos: files_api (A) o local (B). Si no se indica, el de ATTACHMENT_MODE.",
    )] = None,
    settings=Depends(get_settings),
) -> AttachmentMode:
    """El camino elegido en la petición o, si no viene, el configurado en el .env."""
    return attachment_mode or settings.attachment_mode


def require_anthropic_key(settings=Depends(get_settings)) -> str:
    """Los dos caminos llaman a Claude: sin ANTHROPIC_API_KEY no hay nada que hacer."""
    if settings.anthropic_api_key is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Falta ANTHROPIC_API_KEY en el .env. Añádela y reinicia la API.",
        )
    return settings.anthropic_api_key.get_secret_value()


def get_turn_settings(settings=Depends(get_settings)) -> TurnSettings:
    """Copia de config.py los ajustes que necesita un turno."""
    return TurnSettings(
        model=settings.session_llm_model,
        effort=settings.session_llm_effort,
        max_tokens=settings.session_max_tokens,
        pdf_extractor=settings.pdf_extractor,
        max_attachment_chars=settings.max_attachment_chars,
        extraction_timeout_s=settings.attachment_extraction_timeout_s,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", response_model=SessionCreated, status_code=status.HTTP_201_CREATED)
async def create_session(
    request: SessionCreateRequest | None = None,
    store: SessionStore = Depends(get_session_store),
):
    """Crea una sesión y devuelve su session_id.

    Respondemos 201 (Created) en lugar de 200 porque la petición crea algo
    nuevo en el servidor. La sesión nace sin historial. La ficha del proyecto
    nace vacía, salvo que el cuerpo traiga ``{"inherit_from": "<id>"}``:
    entonces hereda una copia de la ficha de esa sesión (viva o caducada).
    Si esa sesión no existe, 404.
    """
    inherit_from = str(request.inherit_from) if request and request.inherit_from else None
    try:
        session = store.create(inherit_from=inherit_from)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay ninguna sesión (viva ni archivada) con ese id de la que heredar la memoria.",
        ) from None
    return SessionCreated(
        session_id=session.session_id,
        inherited_from=session.inherited_from,
        project_metadata=session.metadata,
    )


@router.get("/{session_id}", response_model=SessionState)
async def get_session(
    session: Session = Depends(get_existing_session),
    store: SessionStore = Depends(get_session_store),
):
    """Devuelve la memoria y el historial actuales de una sesión.

    Es de solo lectura: consultar una sesión no cambia ni su historial ni su
    ficha. Sí cuenta como actividad: la marca como usada y reinicia la
    cuenta atrás de su caducidad (expires_at).
    """
    return SessionState(
        session_id=session.session_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        expires_at=store.expires_at(session),
        inherited_from=session.inherited_from,
        max_turns=session.history.max_turns,
        turn_count=session.history.turn_count,
        discarded_turns=session.history.discarded_turns,
        project_metadata=session.metadata,
        history=session.history.messages,
        uploaded_files=session.uploaded_files,
    )


@router.post("/{session_id}/estimate", response_class=EventSourceResponse)
async def estimate_in_session(
    session: Session = Depends(get_existing_session),
    transcript: str = Depends(read_transcript),
    attachments: list[Attachment] = Depends(read_attachments),
    attachment_mode: AttachmentMode = Depends(resolve_attachment_mode),
    api_key: str = Depends(require_anthropic_key),
    turn_settings: TurnSettings = Depends(get_turn_settings),
) -> AsyncIterable[ServerSentEvent]:
    """Estima dentro de una sesión y devuelve la respuesta en streaming (SSE).

    Recibe un formulario multipart/form-data con:

    * ``transcript`` (obligatorio): la transcripción o el siguiente mensaje.
    * ``attachments`` (opcional, varios): PDF o Word con documentación extra.
    * ``attachment_mode`` (opcional): ``files_api`` (Camino A) o ``local`` (B).

    La respuesta es un stream de eventos ``status``, ``delta`` (trozos de la
    estimación), y al final ``done`` o ``error``. El historial de la sesión
    se actualiza solo cuando llega el ``done``.

    Al usar ``response_class=EventSourceResponse`` con un generador, FastAPI
    convierte cada ServerSentEvent al formato del protocolo SSE
    (``event: ...`` / ``data: ...``) y además envía pings periódicos para que
    la conexión no se corte mientras el modelo piensa.
    """
    async for turn_event in stream_estimation_turn(
        session=session,
        transcript=transcript,
        attachments=attachments,
        mode=attachment_mode,
        settings=turn_settings,
        api_key=api_key,
    ):
        yield ServerSentEvent(event=turn_event.event, data=turn_event.data)
