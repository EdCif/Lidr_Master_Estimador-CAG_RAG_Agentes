"""Un turno de conversación de principio a fin (sesión 05).

Este módulo es el "director de orquesta" de POST /sessions/{id}/estimate.
En cada turno hace, en este orden:

1. Prepara el mensaje del usuario con sus adjuntos, por el camino elegido:
   A (Files API de Anthropic) o B (extracción local de texto).
2. Pide al historial el array de mensajes: system prompt + turnos de la
   ventana + la pregunta nueva.
3. Llama al LLM en streaming y va soltando cada trozo de texto según llega.
4. Solo si la respuesta ha llegado completa, guarda el turno en el historial.

Todo lo que pasa se comunica como una secuencia de eventos (``TurnEvent``)
que el router convierte en Server-Sent Events:

* ``status``: qué estamos haciendo ("Extrayendo texto de spec.pdf...").
* ``delta``: un trozo de la estimación. El cliente los va pintando.
* ``done``: fin del turno, con la estimación completa y el estado de la sesión.
* ``error``: algo ha fallado. El turno NO se guarda y el cliente debe
  descartar el texto parcial que hubiera recibido.

Este módulo no sabe nada de HTTP ni de FastAPI. Así se puede probar con
tests sencillos y, si mañana cambiamos de framework, no hay que tocarlo.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from app.services.attachments import extraction_worker, path_a_files_api, path_b_local
from app.services.attachments.common import (
    Attachment,
    AttachmentError,
    LLMProviderError,
    PreparedContent,
    ReplyStats,
)
from app.services.sessions import Session

logger = logging.getLogger(__name__)

AttachmentMode = Literal["files_api", "local"]

# Nombre de cada camino tal como lo contamos al usuario.
PATH_LABELS: dict[str, str] = {
    "files_api": "Camino A · Files API de Anthropic (el modelo lee los PDF directamente)",
    "local": "Camino B · extracción local de texto (pypdf / MarkItDown / python-docx)",
}


@dataclass(frozen=True)
class TurnEvent:
    """Un evento del turno: su tipo y los datos que lo acompañan."""

    event: Literal["status", "delta", "done", "error"]
    data: dict[str, Any]


@dataclass(frozen=True)
class TurnSettings:
    """Los ajustes de config.py que necesita un turno, ya resueltos.

    Los recibimos así, y no leyendo get_settings() aquí dentro, para que los
    tests puedan pasar los suyos sin tocar el .env.
    """

    model: str
    effort: str
    max_tokens: int
    pdf_extractor: str
    max_attachment_chars: int
    # Camino B: segundos como máximo para extraer el texto (0 = sin límite).
    extraction_timeout_s: float = 30.0


async def stream_estimation_turn(
    session: Session,
    transcript: str,
    attachments: list[Attachment],
    mode: AttachmentMode,
    settings: TurnSettings,
    api_key: str,
) -> AsyncIterator[TurnEvent]:
    """Ejecuta un turno completo y lo va contando como eventos."""
    yield TurnEvent("status", {
        "message": f"Turno {session.history.turn_count + 1} · {PATH_LABELS[mode]}.",
        "attachment_mode": mode,
        "model": settings.model,
        "attachments": [attachment.filename for attachment in attachments],
    })

    stats = ReplyStats(model=settings.model)
    prepared: PreparedContent | None = None
    try:
        if mode == "files_api":
            # Camino A: el mismo cliente sube los ficheros y hace la llamada.
            async with path_a_files_api.make_client(api_key) as client:
                if attachments:
                    yield _status("Subiendo los adjuntos a la Files API de Anthropic...")
                prepared = await path_a_files_api.prepare_user_content(
                    client, transcript, attachments, settings.max_attachment_chars
                )
                for note in prepared.notes:
                    yield _status(note)
                messages = session.history.to_messages_list(pending_user_content=prepared.user_content)
                yield _status(_calling_message(messages, settings.model))
                async for text in path_a_files_api.stream_reply(
                    client,
                    messages,
                    model=settings.model,
                    effort=settings.effort,
                    max_tokens=settings.max_tokens,
                    stats=stats,
                ):
                    yield TurnEvent("delta", {"text": text})
        else:
            # Camino B: extraer texto es trabajo de CPU (puede tardar con un
            # PDF largo). Lo hacemos en un proceso aparte con tiempo máximo:
            # si un documento trucado lo deja colgado, se mata ese proceso y
            # la API sigue funcionando (ver attachments/extraction_worker.py).
            # asyncio.to_thread hace que la ESPERA tampoco frene al servidor.
            if attachments:
                yield _status("Extrayendo el texto de los adjuntos en local...")
            if settings.extraction_timeout_s > 0:
                prepared = await asyncio.to_thread(
                    extraction_worker.prepare_user_content_with_time_limit,
                    transcript,
                    attachments,
                    settings.pdf_extractor,
                    settings.max_attachment_chars,
                    settings.extraction_timeout_s,
                )
            else:
                # Sin límite (ATTACHMENT_EXTRACTION_TIMEOUT_S=0): en la propia
                # API, en otro hilo. Solo para depurar.
                prepared = await asyncio.to_thread(
                    path_b_local.prepare_user_content,
                    transcript,
                    attachments,
                    settings.pdf_extractor,
                    settings.max_attachment_chars,
                )
            for note in prepared.notes:
                yield _status(note)
            messages = session.history.to_messages_list(pending_user_content=prepared.user_content)
            yield _status(_calling_message(messages, settings.model))
            async for text in path_b_local.stream_reply(
                messages,
                model=settings.model,
                api_key=api_key,
                max_tokens=settings.max_tokens,
                stats=stats,
            ):
                yield TurnEvent("delta", {"text": text})

    except (AttachmentError, LLMProviderError) as error:
        # Errores previstos, con un mensaje pensado para el usuario.
        yield TurnEvent("error", {"detail": str(error)})
        return
    except Exception:
        # Cualquier otra cosa es un fallo nuestro. Lo dejamos en el log con
        # todos los detalles y al cliente le damos un mensaje genérico.
        logger.exception("Error inesperado en el turno de la sesión %s", session.session_id)
        yield TurnEvent("error", {"detail": "Error inesperado al generar la estimación. Revisa el log del servidor."})
        return

    if stats.incomplete_reason:
        yield TurnEvent("error", {"detail": stats.incomplete_reason})
        return

    # La respuesta ha llegado entera: ahora sí apuntamos el turno. Si
    # hubiéramos fallado antes (o si el cliente hubiera cortado la conexión,
    # que cancela este generador), el historial seguiría como estaba.
    session.history.add_turn(prepared.user_content, stats.final_text)
    session.uploaded_files.extend(prepared.uploaded_files)
    session.touch()

    yield TurnEvent("done", {
        "estimation": stats.final_text,
        "attachment_mode": mode,
        "model": stats.model,
        "usage": {"input_tokens": stats.input_tokens, "output_tokens": stats.output_tokens},
        "session": {
            "session_id": session.session_id,
            "turn_count": session.history.turn_count,
            "max_turns": session.history.max_turns,
            "discarded_turns": session.history.discarded_turns,
        },
        "project_metadata": session.metadata.model_dump(),
    })


def _status(message: str) -> TurnEvent:
    return TurnEvent("status", {"message": message})


def _calling_message(messages: list[dict[str, Any]], model: str) -> str:
    """Frase de estado que cuenta cuántos turnos viajan en esta llamada."""
    previous_turns = sum(1 for message in messages if message["role"] == "user") - 1
    return (
        f"Llamando a {model} con el system prompt, {previous_turns} turno(s) anterior(es) "
        "de la ventana y la pregunta nueva. Opus razona antes de escribir: puede tardar unos segundos."
    )
