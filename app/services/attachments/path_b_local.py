"""Camino B · Extracción local: sacamos el texto aquí y lo pegamos en el prompt.

Cómo funciona
-------------
1. Cada adjunto se convierte en texto en nuestro propio proceso:
   * PDF  -> pypdf (rápido, va bien con PDF "normales" que contienen texto).
     Si pypdf no saca casi nada, probamos con MarkItDown, que usa otro motor
     (pdfminer) y devuelve Markdown con algo más de estructura. También se
     puede elegir MarkItDown directamente con PDF_EXTRACTOR=markitdown.
   * Word -> python-docx, recorriendo párrafos y tablas en su orden.
2. Ese texto se añade al final de la transcripción, cada documento tras un
   separador bien visible:  ``--- attachment: spec.pdf ---``
3. El mensaje resultante es texto plano, así que cualquier LLM lo entiende.
   Lo enviamos con litellm, que habla con muchos proveedores con la misma
   interfaz: cambiar de Claude a otro modelo es cambiar el nombre del modelo.

Qué ganamos y qué perdemos frente al Camino A
---------------------------------------------
* Ganamos control: vemos exactamente qué texto le llega al modelo, podemos
  limpiarlo, limitarlo o trocearlo. Ese troceado (chunking) es justo lo que
  haremos en el módulo 3 con RAG, así que este camino prepara el terreno.
* Ganamos independencia del proveedor: aquí no hay nada propio de Anthropic.
* Perdemos lo que no es texto: imágenes, diagramas de arquitectura, la
  maquetación. Y un PDF escaneado (que en realidad es una foto de cada
  página) no tiene texto que extraer; para esos, mejor el Camino A.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import Any

from docx import Document
from docx.table import Table
from pypdf import PdfReader, PdfWriter
from pypdf.errors import LimitReachedError, PdfReadError

from app.services.attachments.common import (
    Attachment,
    AttachmentError,
    LLMProviderError,
    PreparedContent,
    ReplyStats,
    check_text_limit,
)

# Por debajo de esta media de caracteres por página, damos por hecho que
# pypdf no ha sabido leer el PDF y probamos con MarkItDown. 20 caracteres es
# menos que una línea: una página con texto real tiene cientos.
_MIN_CHARS_PER_PAGE = 20

# Tope del contenido de las páginas ya descomprimido (las instrucciones que
# dibujan el texto). Un PDF de texto normal se queda en unos pocos MB; 50 MB
# deja mucho margen y para los PDF "bomba" antes de que lleguen a MarkItDown.
_MAX_PDF_CONTENT_BYTES = 50 * 1_048_576


# ---------------------------------------------------------------------------
# 1. Extracción de texto
# ---------------------------------------------------------------------------

def extract_pdf_with_pypdf(data: bytes, filename: str = "documento.pdf") -> tuple[str, int]:
    """Extrae el texto de un PDF con pypdf. Devuelve (texto, número de páginas)."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Muchos PDF vienen "cifrados" sin contraseña real, solo para
            # impedir que se editen. decrypt("") los abre; si de verdad tienen
            # contraseña, devuelve 0 y avisamos.
            if not reader.decrypt(""):
                raise AttachmentError(f"«{filename}» está protegido con contraseña.")
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as error:
        raise AttachmentError(f"«{filename}» no es un PDF válido o está dañado.") from error
    return "\n\n".join(pages).strip(), len(pages)


def pdf_as_pypdf_sees_it(
    data: bytes,
    filename: str = "documento.pdf",
    max_content_bytes: int = _MAX_PDF_CONTENT_BYTES,
) -> bytes:
    """Reescribe el PDF con pypdf: solo las páginas que pypdf ha contado.

    El problema que resuelve
    ------------------------
    Las páginas de un PDF las cuenta pypdf al validar el adjunto
    (common.validate_attachment), pero MarkItDown lee el PDF con OTRO lector,
    pdfminer. Un PDF trucado puede enseñar pocas páginas a uno y miles al
    otro: por ejemplo, pdfminer tiene un "modo rescate" que, si el índice de
    páginas está roto, busca páginas sueltas por todo el fichero. Así un
    PDF pasaría el control con 2 páginas y luego costaría como 5.000.

    La solución
    -----------
    MarkItDown nunca recibe el PDF original, sino esta copia, escrita por
    pypdf página a página. Lo que no está en el índice de pypdf no llega a
    la copia, así que los dos lectores ven exactamente el mismo documento y
    el tope de páginas se cumple siempre.

    De paso sumamos lo que ocupa el contenido de cada página descomprimido
    (y el de los "formularios" que dibuja, un recurso que pdfminer también
    lee). pypdf ya corta cualquier flujo que pase de 75 MB, pero pdfminer no
    tiene ningún límite, así que ponemos uno para el total.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise AttachmentError(f"«{filename}» está protegido con contraseña.")
        writer = PdfWriter()
        total = 0
        for page in reader.pages:
            total += _page_content_size(page)
            if total > max_content_bytes:
                raise AttachmentError(
                    f"«{filename}»: el contenido de sus páginas ocupa más de "
                    f"{max_content_bytes / 1_048_576:.0f} MB al descomprimirlo.",
                    status_code=413,
                )
            writer.add_page(page)
        buffer = io.BytesIO()
        writer.write(buffer)
    except AttachmentError:
        raise
    except LimitReachedError as error:
        # pypdf se ha negado a descomprimir algo por ser demasiado grande.
        raise AttachmentError(
            f"«{filename}» contiene partes que ocupan demasiado al descomprimirlas.",
            status_code=413,
        ) from error
    except Exception as error:  # pypdf lanza errores variados con ficheros rotos
        raise AttachmentError(f"«{filename}» no es un PDF válido o está dañado.") from error
    return buffer.getvalue()


def _page_content_size(page: Any) -> int:
    """Bytes del contenido de una página, ya descomprimido, más sus formularios."""
    size = 0
    contents = page.get_contents()
    if contents is not None:
        size += len(contents.get_data())
    resources = page.get("/Resources")
    xobjects = resources.get_object().get("/XObject") if resources is not None else None
    if xobjects is not None:
        for reference in xobjects.get_object().values():
            xobject = reference.get_object()
            if xobject.get("/Subtype") == "/Form":
                size += len(xobject.get_data())
    return size


def extract_pdf_with_markitdown(data: bytes, filename: str = "documento.pdf") -> str:
    """Convierte un PDF a Markdown con MarkItDown.

    Lo importamos aquí dentro y no arriba del todo porque MarkItDown carga
    bastantes dependencias. Así solo pagamos ese tiempo cuando se usa.

    Ojo: a MarkItDown le pasamos la copia de ``pdf_as_pypdf_sees_it``, nunca
    el PDF tal como ha llegado (ver el porqué en esa función).
    """
    from markitdown import MarkItDown

    safe_copy = pdf_as_pypdf_sees_it(data, filename)
    try:
        result = MarkItDown(enable_plugins=False).convert_stream(
            io.BytesIO(safe_copy), file_extension=".pdf"
        )
    except Exception as error:  # MarkItDown no tiene una excepción propia estable
        raise AttachmentError(f"MarkItDown no ha podido leer «{filename}».") from error
    return (result.markdown or "").strip()


def extract_docx_text(data: bytes, filename: str = "documento.docx") -> str:
    """Extrae el texto de un Word (.docx) con python-docx, en el orden del documento.

    Recorremos párrafos y tablas tal como aparecen. Los títulos se marcan con
    "#" al estilo Markdown para que el modelo vea la estructura del
    documento, y cada fila de una tabla sale como "celda | celda | celda".
    """
    try:
        document = Document(io.BytesIO(data))
    except Exception as error:  # python-docx lanza errores variados con ficheros rotos
        raise AttachmentError(f"«{filename}» no es un .docx válido o está dañado.") from error

    lines: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    lines.append(" | ".join(cells))
            lines.append("")
            continue

        text = block.text.strip()
        if not text:
            continue
        style = (block.style.name if block.style is not None else "") or ""
        if style == "Title":
            lines.append(f"# {text}")
        elif style.startswith("Heading"):
            # "Heading 2" -> "## ". Si el nivel no es un número, usamos "##".
            level = style.removeprefix("Heading").strip()
            hashes = "#" * (int(level) if level.isdigit() else 2)
            lines.append(f"{hashes} {text}")
        else:
            lines.append(text)
    return "\n".join(lines).strip()


def extract_text(attachment: Attachment, pdf_extractor: str = "pypdf") -> tuple[str, str]:
    """Extrae el texto de un adjunto. Devuelve (texto, herramienta usada)."""
    if attachment.kind == "docx":
        text, method = extract_docx_text(attachment.data, attachment.filename), "python-docx"
    elif pdf_extractor == "markitdown":
        text, method = extract_pdf_with_markitdown(attachment.data, attachment.filename), "MarkItDown"
    else:
        text, pages = extract_pdf_with_pypdf(attachment.data, attachment.filename)
        method = "pypdf"
        if len("".join(text.split())) < _MIN_CHARS_PER_PAGE * max(pages, 1):
            # pypdf apenas ha sacado nada: segunda oportunidad con otro motor.
            text, method = extract_pdf_with_markitdown(attachment.data, attachment.filename), "MarkItDown (pypdf no extrajo texto)"

    if not text.strip():
        hint = (
            " Parece un PDF escaneado (imágenes, sin texto). Prueba con el Camino A "
            "(attachment_mode=files_api): ahí Claude lee el PDF como imagen."
            if attachment.kind == "pdf"
            else ""
        )
        raise AttachmentError(f"«{attachment.filename}» no tiene texto que se pueda extraer.{hint}")
    return text, method


# ---------------------------------------------------------------------------
# 2. Construcción del mensaje: transcripción + adjuntos con separador
# ---------------------------------------------------------------------------

def build_prompt_with_attachments(transcript: str, documents: list[tuple[str, str]]) -> str:
    """Pega cada documento al final de la transcripción tras un separador claro.

    ``documents`` es una lista de (nombre de fichero, texto). Resultado:

        <transcripción>

        --- attachment: spec.pdf ---
        <texto de spec.pdf>

        --- attachment: propuesta.docx ---
        <texto de propuesta.docx>

    El separador le deja claro al modelo dónde acaba la reunión y dónde
    empieza cada documento, y le permite citarlos por su nombre.
    """
    parts = [transcript.strip()]
    for filename, text in documents:
        parts.append(f"--- attachment: {filename} ---\n{text.strip()}")
    return "\n\n".join(parts)


def prepare_user_content(
    transcript: str,
    attachments: list[Attachment],
    pdf_extractor: str = "pypdf",
    max_chars: int = 300_000,
) -> PreparedContent:
    """Extrae todos los adjuntos y monta el mensaje del usuario (texto plano).

    Es una función normal (no async) porque extraer texto es trabajo de CPU.
    El servicio no la llama directamente: la ejecuta en un proceso aparte
    con tiempo máximo (``extraction_worker.py``), para que un documento
    trucado no pueda dejar colgada la API.

    Si el texto de los adjuntos supera ``max_chars`` NO lo recortamos a
    escondidas: devolvemos un error. Un recorte silencioso haría que el
    modelo estimara con medio documento sin que nadie se enterara.
    """
    documents: list[tuple[str, str]] = []
    notes: list[str] = []
    for attachment in attachments:
        text, method = extract_text(attachment, pdf_extractor)
        documents.append((attachment.filename, text))
        notes.append(f"{attachment.filename}: {len(text):,} caracteres extraídos con {method}.".replace(",", "."))

    check_text_limit(sum(len(text) for _, text in documents), max_chars)
    return PreparedContent(
        user_content=build_prompt_with_attachments(transcript, documents),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 3. Llamada al LLM en streaming con litellm
# ---------------------------------------------------------------------------

def to_text_only_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convierte a texto los mensajes que vengan en bloques (de turnos del Camino A).

    Si en una misma sesión se usó antes el Camino A, el historial guarda
    mensajes con bloques "document" que apuntan a ficheros de la Files API
    de Anthropic. litellm (y cualquier otro proveedor) no sabría qué hacer
    con ellos, así que dejamos el texto y cambiamos cada documento por una
    nota "[documento adjunto en un turno anterior: spec.pdf]".
    """
    converted: list[dict[str, Any]] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            pieces = []
            for block in content:
                if block.get("type") == "text":
                    pieces.append(block.get("text", ""))
                elif block.get("type") == "document":
                    pieces.append(f"[documento adjunto en un turno anterior: {block.get('title', 'sin nombre')}]")
            content = "\n\n".join(pieces)
        converted.append({"role": message["role"], "content": content})
    return converted


async def stream_reply(
    messages: list[dict[str, Any]],
    *,
    model: str,
    api_key: str,
    max_tokens: int,
    stats: ReplyStats,
) -> AsyncIterator[str]:
    """Envía la conversación al LLM con litellm y va devolviendo el texto a trozos.

    litellm usa el formato de OpenAI (el system prompt va dentro de la lista
    de mensajes), que es justo lo que devuelve to_messages_list(). El prefijo
    "anthropic/" del modelo le dice con qué proveedor hablar.

    El esfuerzo de razonamiento no se lo pasamos: litellm no tiene un
    parámetro común para eso, y el valor por defecto del modelo del LLM ya es
    "medium", el mismo que fijamos en el Camino A. Así los dos caminos
    estiman en igualdad de condiciones.
    """
    # Importamos litellm aquí dentro: al importarse tarda un par de segundos
    # y puede intentar descargar su tabla de precios. Así no frena el
    # arranque de la API ni los tests, que no lo usan.
    import litellm

    chunks: list[str] = []
    finish_reason: str | None = None
    try:
        response = await litellm.acompletion(
            model=f"anthropic/{model}",
            messages=to_text_only_messages(messages),
            api_key=api_key,
            max_tokens=max_tokens,
            stream=True,
            # Pide que el último trozo traiga el consumo de tokens.
            stream_options={"include_usage": True},
        )
        async for chunk in response:
            if chunk.choices:
                choice = chunk.choices[0]
                if choice.delta and choice.delta.content:
                    chunks.append(choice.delta.content)
                    yield choice.delta.content
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
            usage = getattr(chunk, "usage", None)
            if usage:
                stats.input_tokens = usage.prompt_tokens
                stats.output_tokens = usage.completion_tokens
    except litellm.AuthenticationError as error:
        raise LLMProviderError("La ANTHROPIC_API_KEY no es válida. Revísala en el .env.") from error
    except litellm.RateLimitError as error:
        raise LLMProviderError("Se ha alcanzado el límite de peticiones del proveedor. Espera y reintenta.") from error
    except (litellm.APIConnectionError, litellm.Timeout) as error:
        raise LLMProviderError("No se ha podido conectar con el proveedor del LLM. Reintenta.") from error
    except litellm.BadRequestError as error:
        raise LLMProviderError(f"El proveedor ha rechazado la petición: {error.message}") from error
    except litellm.APIError as error:
        raise LLMProviderError("El proveedor del LLM ha devuelto un error. Reintenta en unos momentos.") from error

    stats.final_text = "".join(chunks)
    # litellm traduce los motivos de parada al vocabulario de OpenAI / Anthropic:
    # "stop" = terminó bien, "length" = se quedó sin tokens, "content_filter"
    # = el modelo se negó a responder.
    if finish_reason == "length":
        stats.incomplete_reason = "La respuesta se cortó por el límite de tokens (SESSION_MAX_TOKENS)."
    elif finish_reason == "content_filter":
        stats.incomplete_reason = "El modelo ha rechazado generar esta respuesta."
    elif not stats.final_text.strip():
        stats.incomplete_reason = "El modelo ha devuelto una respuesta vacía."
