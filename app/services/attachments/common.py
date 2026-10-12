"""Lo que comparten los dos caminos: qué es un adjunto y cómo lo validamos.

Antes de gastar una sola llamada al LLM comprobamos que cada fichero es de
verdad un PDF o un Word (.docx) y que no se pasa de tamaño. Lo hacemos aquí,
al recibir la petición, para poder responder con un error HTTP claro (413,
415...) en vez de descubrir el problema a mitad del streaming.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Literal

AttachmentKind = Literal["pdf", "docx"]

# Tipo MIME de cada clase de adjunto: es la etiqueta que acompaña al fichero
# cuando se sube a una API ("esto es un PDF").
MEDIA_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Los primeros bytes de un fichero ("número mágico") delatan su formato real,
# aunque alguien le cambie la extensión. Todo PDF empieza por "%PDF-". Un
# .docx es en realidad un ZIP con XML dentro, y todo ZIP empieza por "PK".
_MAGIC_BYTES: dict[str, bytes] = {"pdf": b"%PDF-", "docx": b"PK\x03\x04"}


class AttachmentError(ValueError):
    """Un adjunto no se puede usar. Lleva el código HTTP con el que responder.

    413 = demasiado grande, 415 = formato no admitido, 422 = el fichero es
    del tipo correcto pero no sirve (vacío, sin texto, roto...).
    """

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMProviderError(RuntimeError):
    """Algo ha fallado al hablar con el proveedor del LLM.

    Cada camino usa una librería distinta (el SDK de Anthropic en el A,
    litellm en el B) y cada una tiene sus propias excepciones. Las
    traducimos a esta, con un mensaje en castellano que se puede enseñar al
    usuario, para que el resto del código no tenga que conocer las dos.
    """


@dataclass
class PreparedContent:
    """El mensaje del usuario ya listo para el LLM, tras procesar los adjuntos.

    * ``user_content``: un texto (Camino B, o cuando no hay adjuntos) o una
      lista de bloques documento + texto (Camino A).
    * ``notes``: frases que contamos al usuario mientras esperamos ("spec.pdf:
      4.210 caracteres extraídos con pypdf"). Se envían como eventos de estado.
    * ``uploaded_files``: en el Camino A, qué ficheros se subieron y con qué
      file_id, para poder consultarlos o borrarlos después.
    """

    user_content: str | list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)
    uploaded_files: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ReplyStats:
    """Datos de la respuesta que solo se conocen cuando el streaming termina.

    Un generador va soltando trozos de texto con ``yield`` y no puede
    devolver nada más por el camino. Así que le pasamos este objeto y lo
    rellena al acabar: tokens consumidos, el texto final y, si la respuesta
    no salió completa, el motivo (``incomplete_reason``).
    """

    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    final_text: str | None = None
    incomplete_reason: str | None = None


@dataclass(frozen=True)
class Attachment:
    """Un fichero adjunto ya leído y validado.

    Guardamos los bytes en memoria (no en disco) porque son ficheros pequeños
    y solo los necesitamos mientras dura la petición.
    """

    filename: str
    kind: AttachmentKind
    data: bytes

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self.kind]


def sanitize_filename(raw_name: str | None) -> str:
    """Deja el nombre del fichero listo para mostrarlo y meterlo en el prompt.

    El nombre lo manda el cliente, así que no nos fiamos de él:

    * Quitamos cualquier ruta ("C:\\docs\\spec.pdf" o "../../spec.pdf" pasan
      a ser "spec.pdf").
    * Quitamos saltos de línea y caracteres de control. Un nombre con un
      salto de línea podría romper el separador "--- attachment: ... ---" del
      Camino B, o colar texto que parezca una instrucción.
    * Lo recortamos a 120 caracteres.
    """
    name = PurePath((raw_name or "").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:120] or "adjunto"


def detect_kind(filename: str, data: bytes) -> AttachmentKind:
    """Decide si el fichero es un PDF o un Word mirando extensión Y contenido.

    Exigimos que las dos cosas cuadren. Un .doc antiguo (formato binario de
    Word 97-2003) no se admite: python-docx no sabe leerlo. En ese caso el
    mensaje de error le dice al usuario que lo guarde como .docx o PDF.
    """
    extension = PurePath(filename).suffix.lower().lstrip(".")
    if extension not in _MAGIC_BYTES:
        raise AttachmentError(
            f"«{filename}»: solo se admiten PDF (.pdf) y Word (.docx). "
            "Si es un .doc antiguo, guárdalo como .docx o PDF.",
            status_code=415,
        )
    if not data.startswith(_MAGIC_BYTES[extension]):
        raise AttachmentError(
            f"«{filename}» tiene extensión .{extension} pero su contenido no lo es.",
            status_code=415,
        )
    return extension  # type: ignore[return-value]  (ya sabemos que es "pdf" o "docx")


def validate_attachment(raw_name: str | None, data: bytes, max_bytes: int) -> Attachment:
    """Valida un fichero recibido y lo convierte en un ``Attachment``."""
    filename = sanitize_filename(raw_name)
    if not data:
        raise AttachmentError(f"«{filename}» está vacío.")
    if len(data) > max_bytes:
        raise AttachmentError(
            f"«{filename}» ocupa {len(data) / 1_048_576:.1f} MB y el máximo es "
            f"{max_bytes / 1_048_576:.0f} MB.",
            status_code=413,
        )
    return Attachment(filename=filename, kind=detect_kind(filename, data), data=data)
