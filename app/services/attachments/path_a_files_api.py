"""Camino A · Multimodal directo: el PDF se sube a Anthropic y Claude lo lee entero.

Cómo funciona
-------------
1. Cada PDF se sube tal cual a la Files API de Anthropic
   (``client.files.upload``). Anthropic lo guarda y nos devuelve un
   identificador, el ``file_id``.
2. En el mensaje del usuario no pegamos el texto del PDF: ponemos un bloque
   ``document`` que apunta a ese file_id, y detrás la transcripción.
3. Claude abre el documento por su cuenta: lee el texto y además "ve" cada
   página como imagen, así que entiende tablas, diagramas de arquitectura y
   PDF escaneados, que en el Camino B se perderían.

Los Word (.docx), un caso aparte
--------------------------------
Los bloques ``document`` de la API de Anthropic aceptan PDF y texto plano,
pero no .docx. Para no rechazarlos, sacamos su texto en local (con la misma
función del Camino B) y subimos ese texto como un fichero .txt. Es la única
parte de este camino que no es "multimodal directo", y lo contamos en los
mensajes de estado para que no pase desapercibido.

Qué ganamos y qué perdemos frente al Camino B
---------------------------------------------
* Ganamos sencillez y comprensión: menos código y el modelo ve el documento
  completo, imágenes incluidas.
* Perdemos independencia: la Files API y los bloques ``document`` son de
  Anthropic. Cambiar de proveedor obligaría a reescribir este módulo.
* Perdemos control: no sabemos qué ha "leído" exactamente el modelo, y cada
  página de PDF cuenta como texto + imagen, así que suele gastar más tokens.

Los ficheros subidos caducan solos a las 24 horas (``expires_in_seconds``).
Mientras tanto se pueden reutilizar: si el turno sigue dentro de la ventana
del historial, la siguiente llamada vuelve a apuntar al mismo file_id sin
tener que subirlo otra vez.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app.services.attachments.common import (
    Attachment,
    AttachmentError,
    LLMProviderError,
    PreparedContent,
    ReplyStats,
    check_text_limit,
)
from app.services.attachments.path_b_local import extract_docx_text

# Los ficheros se borran solos de Anthropic pasado este tiempo. Las sesiones
# viven en la memoria del proceso y se pierden al reiniciar, así que 24 horas
# sobran. La API admite entre 1 hora (3600) y 90 días.
FILES_EXPIRE_SECONDS = 24 * 60 * 60

# Si los filtros de seguridad de Opus 5.5 rechazan una petición, con
# fallbacks="default" la API la repite automáticamente con el modelo que
# Anthropic recomienda para ese caso, en la misma llamada. Es una función en
# beta, por eso va con su cabecera.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def make_client(api_key: str) -> anthropic.AsyncAnthropic:
    """Crea el cliente asíncrono de Anthropic.

    Se usa con ``async with``, que cierra sus conexiones al terminar aunque
    haya un error. El SDK ya reintenta solo los fallos pasajeros (429, 5xx,
    cortes de red) dos veces antes de rendirse.
    """
    return anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)


async def prepare_user_content(
    client: anthropic.AsyncAnthropic,
    transcript: str,
    attachments: list[Attachment],
    max_chars: int = 300_000,
) -> PreparedContent:
    """Sube los adjuntos y monta el mensaje: bloques ``document`` + transcripción.

    Sin adjuntos, el mensaje es la transcripción tal cual (un texto). Con
    adjuntos, es una lista de bloques: primero los documentos y al final la
    transcripción. La documentación de Anthropic recomienda ese orden, con
    los documentos antes de la pregunta.

    Va en dos pasadas:

    1. Convertimos los Word a texto y comprobamos que, todos juntos, no
       pasan de ``max_chars`` (el mismo límite que el Camino B). Si se
       pasan, paramos aquí, sin haber subido nada a Anthropic.
    2. Subimos los ficheros y montamos los bloques.

    Los PDF no cuentan para ``max_chars`` porque no sacamos su texto: los
    lee Anthropic. A ellos les ponen límite las páginas, que ya se
    comprobaron al recibir la petición (common.validate_attachment).
    """
    if not attachments:
        return PreparedContent(user_content=transcript)

    # Pasada 1. Extraer el texto de un Word es trabajo de CPU: con
    # asyncio.to_thread lo hacemos en otro hilo, para que el servidor siga
    # atendiendo otras peticiones mientras tanto (si no, un Word grande lo
    # dejaría congelado unos segundos).
    docx_texts: dict[int, str] = {}
    for position, attachment in enumerate(attachments):
        if attachment.kind == "docx":
            text = await asyncio.to_thread(extract_docx_text, attachment.data, attachment.filename)
            if not text:
                raise AttachmentError(f"«{attachment.filename}» no tiene texto.")
            docx_texts[position] = text
    check_text_limit(sum(len(text) for text in docx_texts.values()), max_chars)

    # Pasada 2. Subir y montar los bloques.
    blocks: list[dict[str, Any]] = []
    notes: list[str] = []
    uploaded: list[dict[str, str]] = []
    try:
        for position, attachment in enumerate(attachments):
            if attachment.kind == "pdf":
                file = (attachment.filename, attachment.data, attachment.media_type)
                note = "subido a la Files API; Claude leerá el PDF completo, imágenes incluidas."
            else:
                text = docx_texts[position]
                file = (f"{attachment.filename}.txt", text.encode("utf-8"), "text/plain")
                note = "la API no acepta .docx: convertido a texto en local y subido como .txt."

            metadata = await client.files.upload(file=file, expires_in_seconds=FILES_EXPIRE_SECONDS)
            blocks.append({
                "type": "document",
                "source": {"type": "file", "file_id": metadata.id},
                # El título le dice al modelo cómo se llama el documento, para
                # que pueda referirse a él en la estimación.
                "title": attachment.filename,
            })
            uploaded.append({"filename": attachment.filename, "file_id": metadata.id})
            notes.append(f"{attachment.filename}: {note} (file_id {metadata.id})")
    except anthropic.APIError as error:
        raise _provider_error(error) from error

    blocks.append({"type": "text", "text": transcript})
    return PreparedContent(user_content=blocks, notes=notes, uploaded_files=uploaded)


async def stream_reply(
    client: anthropic.AsyncAnthropic,
    messages: list[dict[str, Any]],
    *,
    model: str,
    effort: str,
    max_tokens: int,
    stats: ReplyStats,
) -> AsyncIterator[str]:
    """Envía la conversación a Claude y va devolviendo el texto a trozos.

    to_messages_list() nos da los mensajes en formato OpenAI, con el system
    prompt como primer elemento de la lista. La API de Anthropic lo quiere
    aparte, en el parámetro ``system``, así que aquí lo separamos.

    Usamos ``client.beta.messages`` (y no ``client.messages``) solo porque el
    parámetro ``fallbacks`` está en beta. Lo demás es la API normal.
    """
    if not messages or messages[0]["role"] != "system":
        raise ValueError("El primer mensaje tiene que ser el system prompt.")
    system_prompt, conversation = messages[0]["content"], messages[1:]

    try:
        async with client.beta.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=conversation,
            # Opus 5.5 siempre razona antes de contestar; "effort" regula
            # cuánto. Su valor por defecto es "medium" y lo dejamos explícito.
            output_config={"effort": effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            # text_stream solo entrega el texto de la respuesta. El
            # razonamiento interno de Opus 5.5 llega en otros bloques que aquí
            # no nos interesan; mientras dura, el usuario solo ve los mensajes
            # de estado.
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
    except anthropic.APIError as error:
        raise _provider_error(error) from error

    stats.model = final.model
    stats.input_tokens = final.usage.input_tokens
    stats.output_tokens = final.usage.output_tokens
    stats.final_text = "".join(block.text for block in final.content if block.type == "text")
    if final.stop_reason == "refusal":
        stats.incomplete_reason = "El modelo ha rechazado generar esta respuesta."
    elif final.stop_reason == "max_tokens":
        stats.incomplete_reason = "La respuesta se cortó por el límite de tokens (SESSION_MAX_TOKENS)."
    elif not stats.final_text.strip():
        stats.incomplete_reason = "El modelo ha devuelto una respuesta vacía."


def _provider_error(error: anthropic.APIError) -> LLMProviderError:
    """Traduce las excepciones del SDK de Anthropic a un mensaje para el usuario.

    Van de la más concreta a la más general: si pusiéramos APIStatusError
    primero, se tragaría a todas las demás.
    """
    if isinstance(error, anthropic.AuthenticationError):
        return LLMProviderError("La ANTHROPIC_API_KEY no es válida. Revísala en el .env.")
    if isinstance(error, anthropic.RateLimitError):
        return LLMProviderError("Se ha alcanzado el límite de peticiones de Anthropic. Espera y reintenta.")
    if isinstance(error, anthropic.BadRequestError):
        return LLMProviderError(f"Anthropic ha rechazado la petición: {error.message}")
    if isinstance(error, anthropic.APIStatusError):
        return LLMProviderError(f"Anthropic ha devuelto un error {error.status_code}. Reintenta en unos momentos.")
    if isinstance(error, anthropic.APIConnectionError):
        return LLMProviderError("No se ha podido conectar con Anthropic. Comprueba la conexión y reintenta.")
    return LLMProviderError("Error inesperado al hablar con Anthropic.")
