"""Lo que comparten los dos caminos: qué es un adjunto y cómo lo validamos.

Antes de gastar una sola llamada al LLM comprobamos que cada fichero es de
verdad un PDF o un Word (.docx) y que no se pasa de tamaño. Lo hacemos aquí,
al recibir la petición, para poder responder con un error HTTP claro (413,
415...) en vez de descubrir el problema a mitad del streaming.

Por qué no basta con limitar los megas del fichero
--------------------------------------------------
Un fichero pequeño puede costar muchísimo trabajo al abrirlo:

* Un .docx es un ZIP. Uno de 10 MB puede "inflarse" hasta varios gigas al
  descomprimirlo (lo que se conoce como bomba ZIP) y dejar al servidor sin
  memoria. Por eso, antes de abrirlo, sumamos lo que ocuparía descomprimido.
* Un PDF puede tener miles de páginas casi vacías en muy poco espacio, y
  extraer el texto de cada una cuesta tiempo de CPU. Por eso contamos las
  páginas antes de extraer nada.

Las dos comprobaciones son baratas: leen el índice del fichero, no su
contenido. Si se pasa, respondemos 413 sin haber gastado nada.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Literal

from pypdf import PdfReader

AttachmentKind = Literal["pdf", "docx"]

# Topes por defecto (config.py permite cambiarlos desde el .env).
# 300 páginas cubren con holgura una especificación técnica real; la API de
# Anthropic admite hasta 600, pero aquí no esperamos documentos así.
DEFAULT_MAX_PDF_PAGES = 300
# Un Word con texto y alguna imagen rara vez pasa de unos pocos MB ya
# descomprimido. 50 MB deja mucho margen y corta las bombas ZIP.
DEFAULT_MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1_048_576

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


def count_pdf_pages(filename: str, data: bytes) -> int:
    """Cuenta las páginas de un PDF sin extraer su texto.

    pypdf solo recorre el índice de páginas, que es muy rápido aunque el PDF
    sea enorme. Si el PDF tiene contraseña, o está tan roto que no se puede
    abrir, lo rechazamos ya aquí: ninguno de los dos caminos podría leerlo.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        # Muchos PDF vienen "cifrados" sin contraseña real, solo para impedir
        # que se editen. decrypt("") los abre; si de verdad la tienen, devuelve 0.
        if reader.is_encrypted and not reader.decrypt(""):
            raise AttachmentError(f"«{filename}» está protegido con contraseña.")
        return len(reader.pages)
    except AttachmentError:
        raise
    except Exception as error:  # pypdf lanza errores variados con ficheros rotos
        raise AttachmentError(f"«{filename}» no es un PDF válido o está dañado.") from error


def docx_uncompressed_size(filename: str, data: bytes) -> int:
    """Lo que ocuparía el .docx descomprimido, leído del índice del ZIP.

    Un ZIP guarda al final un índice con el tamaño original de cada fichero
    de dentro. Sumarlos no descomprime nada. ¿Y si el índice miente y dice
    menos de lo real? No importa: el módulo zipfile de Python nunca entrega
    más bytes de los que dice el índice, así que esa suma es un tope fiable.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as error:
        raise AttachmentError(f"«{filename}» no es un .docx válido o está dañado.") from error


def check_text_limit(total_chars: int, max_chars: int) -> None:
    """Rechaza los adjuntos si su texto, todo junto, supera ``max_chars``.

    NO recortamos a escondidas: un recorte silencioso haría que el modelo
    estimara con medio documento sin que nadie se enterara. Lo usan los dos
    caminos, para que el límite y el mensaje sean los mismos.
    """
    if total_chars > max_chars:
        raise AttachmentError(
            f"Los adjuntos suman {total_chars:,} caracteres de texto y el máximo es "
            f"{max_chars:,}. Quita algún documento o envía solo las partes relevantes.".replace(",", "."),
            status_code=413,
        )


def validate_attachment(
    raw_name: str | None,
    data: bytes,
    max_bytes: int,
    max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES,
    max_docx_uncompressed_bytes: int = DEFAULT_MAX_DOCX_UNCOMPRESSED_BYTES,
) -> Attachment:
    """Valida un fichero recibido y lo convierte en un ``Attachment``.

    Por orden: que no esté vacío, que no pese demasiado, que sea de verdad
    un PDF o un Word y, por último, que al abrirlo no vaya a costar más de la
    cuenta (páginas del PDF o tamaño descomprimido del Word).
    """
    filename = sanitize_filename(raw_name)
    if not data:
        raise AttachmentError(f"«{filename}» está vacío.")
    if len(data) > max_bytes:
        raise AttachmentError(
            f"«{filename}» ocupa {len(data) / 1_048_576:.1f} MB y el máximo es "
            f"{max_bytes / 1_048_576:.0f} MB.",
            status_code=413,
        )
    kind = detect_kind(filename, data)

    if kind == "pdf":
        pages = count_pdf_pages(filename, data)
        if pages > max_pdf_pages:
            raise AttachmentError(
                f"«{filename}» tiene {pages} páginas y el máximo es {max_pdf_pages}. "
                "Envía solo las partes que importan para la estimación.",
                status_code=413,
            )
    else:
        inflated = docx_uncompressed_size(filename, data)
        if inflated > max_docx_uncompressed_bytes:
            raise AttachmentError(
                f"«{filename}» ocuparía {inflated / 1_048_576:.0f} MB al abrirlo y el máximo "
                f"es {max_docx_uncompressed_bytes / 1_048_576:.0f} MB.",
                status_code=413,
            )

    return Attachment(filename=filename, kind=kind, data=data)
