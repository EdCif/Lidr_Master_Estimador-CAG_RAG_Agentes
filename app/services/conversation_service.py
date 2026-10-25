"""Un turno de conversación de principio a fin (sesión 05).

Este módulo es el "director de orquesta" de POST /sessions/{id}/estimate.
En cada turno hace, en este orden:

1. Prepara el mensaje del usuario con sus adjuntos, por el camino elegido:
   A (Files API de Anthropic) o B (extracción local de texto).
2. Pide al historial el array de mensajes: system prompt + turnos de la
   ventana + la pregunta nueva.
3. Llama al LLM en streaming y va soltando cada trozo de texto según llega.
4. Solo si la respuesta ha llegado completa, guarda el turno en el historial.
5. Actualiza la memoria del proyecto (project_metadata) con los hechos del
   turno, usando el extractor heurístico (``metadata_extractor.py``).
6. (Sesión 06) Deja la "ficha técnica" del turno, ``turn_observed``: tokens,
   coste, latencia, tamaño de la ventana... (``turn_observer.py``). Se hace
   también cuando el turno acaba en error, para que ninguno quede sin medir.

Todo lo que pasa se comunica como una secuencia de eventos (``TurnEvent``)
que el router convierte en Server-Sent Events:

* ``status``: qué estamos haciendo ("Extrayendo texto de spec.pdf...").
* ``delta``: un trozo de la estimación. El cliente los va pintando.
* ``done``: fin del turno, con la estimación completa, el estado de la
  sesión y la observación ``turn_observed``.
* ``error``: algo ha fallado. El turno NO se guarda y el cliente debe
  descartar el texto parcial que hubiera recibido. Lleva ``detail`` (el
  mensaje), ``retryable`` (si merece la pena repetirlo: ha fallado el
  proveedor, no la petición) y también ``turn_observed``.

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
from app.services.metadata_extractor import update_metadata_from_turn
from app.services.sessions import Session
from app.services.turn_observer import TurnTimer, observe_turn

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
    # Sesión 06: el cronómetro empieza ya, y el número de turno cuenta también
    # los turnos que la ventana haya descartado (el séptimo turno es el 7,
    # aunque en la ventana solo quepan 6).
    timer = TurnTimer()
    turn_index = session.history.discarded_turns + session.history.turn_count + 1
    yield TurnEvent("status", {
        "message": f"Turno {turn_index} · {PATH_LABELS[mode]}.",
        "attachment_mode": mode,
        "model": settings.model,
        "attachments": [attachment.filename for attachment in attachments],
    })

    stats = ReplyStats(model=settings.model)
    prepared: PreparedContent | None = None
    messages: list[dict[str, Any]] | None = None

    def observe(outcome: Literal["done", "error"], error_detail: str | None = None) -> dict[str, Any]:
        """Deja la ficha técnica del turno y la devuelve lista para el evento.

        Es una función dentro de la función para poder leer cómo han quedado
        ``prepared``, ``messages`` y ``stats`` en el momento de llamarla: al
        final del turno, o a mitad de camino si algo ha fallado.
        """
        observation = observe_turn(
            session,
            turn_index=turn_index,
            transcript=transcript,
            attachment_chars=prepared.attachment_chars if prepared else 0,
            attachments_count=len(attachments),
            stats=stats,
            attachment_mode=mode,
            configured_model=settings.model,
            timer=timer,
            messages_sent=len(messages) if messages is not None else None,
            outcome=outcome,
            error_detail=error_detail,
        )
        return observation.model_dump(mode="json")

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
                timer.mark_llm_start()
                async for text in path_a_files_api.stream_reply(
                    client,
                    messages,
                    model=settings.model,
                    effort=settings.effort,
                    max_tokens=settings.max_tokens,
                    stats=stats,
                ):
                    timer.mark_first_delta()
                    yield TurnEvent("delta", {"text": text})
                timer.mark_llm_end()
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
            timer.mark_llm_start()
            async for text in path_b_local.stream_reply(
                messages,
                model=settings.model,
                api_key=api_key,
                max_tokens=settings.max_tokens,
                stats=stats,
            ):
                timer.mark_first_delta()
                yield TurnEvent("delta", {"text": text})
            timer.mark_llm_end()

    except (AttachmentError, LLMProviderError) as error:
        # Errores previstos, con un mensaje pensado para el usuario. Los del
        # proveedor (caído, límite de peticiones...) pueden ser pasajeros: se
        # marcan como "retryable" para que quien llama sepa que puede repetir.
        # Repetir es seguro: un turno fallido no ha tocado el historial.
        yield _error(str(error), isinstance(error, LLMProviderError), observe("error", str(error)))
        return
    except Exception:
        # Cualquier otra cosa es un fallo nuestro. Lo dejamos en el log con
        # todos los detalles y al cliente le damos un mensaje genérico.
        logger.exception("Error inesperado en el turno de la sesión %s", session.session_id)
        detail = "Error inesperado al generar la estimación. Revisa el log del servidor."
        yield _error(detail, False, observe("error", detail))
        return

    if stats.incomplete_reason:
        yield _error(stats.incomplete_reason, False, observe("error", stats.incomplete_reason))
        return

    # La respuesta ha llegado entera: ahora sí apuntamos el turno. Si
    # hubiéramos fallado antes (o si el cliente hubiera cortado la conexión,
    # que cancela este generador), el historial seguiría como estaba.
    session.history.add_turn(prepared.user_content, stats.final_text)
    session.uploaded_files.extend(prepared.uploaded_files)
    session.touch()

    # La memoria: leemos los hechos del turno (lo que ha dicho el cliente y
    # el bloque <hechos_del_proyecto> de la respuesta) y los fundimos con la
    # ficha que ya teníamos. En el siguiente turno, el system prompt los
    # llevará en su bloque <project_metadata>. Si el extractor fallara por un
    # error nuestro, no perdemos el turno: la ficha se queda como estaba.
    yield _status("Actualizando la memoria del proyecto (project_metadata)...")
    try:
        session.metadata = update_metadata_from_turn(session.metadata, transcript, stats.final_text)
    except Exception:
        logger.exception("No se ha podido actualizar la memoria de la sesión %s", session.session_id)

    # Sesión 06: la ficha técnica se toma ahora, con el historial y la memoria
    # ya actualizados, justo antes del evento final.
    turn_observed = observe("done")

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
        "turn_observed": turn_observed,
    })


def _status(message: str) -> TurnEvent:
    return TurnEvent("status", {"message": message})


def _error(detail: str, retryable: bool, turn_observed: dict[str, Any]) -> TurnEvent:
    """Evento de error: el mensaje, si se puede reintentar y la ficha del turno."""
    return TurnEvent("error", {"detail": detail, "retryable": retryable, "turn_observed": turn_observed})


def _calling_message(messages: list[dict[str, Any]], model: str) -> str:
    """Frase de estado que cuenta cuántos turnos viajan en esta llamada."""
    previous_turns = sum(1 for message in messages if message["role"] == "user") - 1
    return (
        f"Llamando a {model} con el system prompt, {previous_turns} turno(s) anterior(es) "
        "de la ventana y la pregunta nueva. Opus razona antes de escribir: puede tardar unos segundos."
    )
