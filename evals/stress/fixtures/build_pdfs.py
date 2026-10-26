"""Fabrica los PDF de prueba del stress test (sesión 6): 5, 20, 50 y 100 KB.

Para qué sirven
---------------
Para medir qué le pasa al estimador cuando el cliente adjunta documentos
cada vez más grandes: cuánto tarda, cuánto cuesta y si de verdad usa lo que
pone el documento. Hace falta que los documentos tengan tamaños exactos y
salgan siempre iguales, para que las medidas se puedan comparar.

Qué llevan dentro
-----------------
* Relleno: un anexo con la "normativa interna de uso de las salas"
  ("Artículo 12. Las salas deben quedar ordenadas..."). Parece un documento
  real pero no pide funciones nuevas, así que no hincha la estimación.
* Tres datos "plantados" (``PLANTED_FACTS``) que cambian la estimación si
  el modelo los lee: la integración con SAP (al principio), la migración de
  12.000 reservas (en medio) y la certificación ISO 27001 (al final). El
  runner busca esos datos en la respuesta del modelo: es el "recall del
  adjunto". Las tres posiciones dejan ver si el modelo se fija menos en el
  centro del documento (el efecto "lost in the middle" que cita la lección
  de calidad del dato).

Qué es "un PDF de 20 KB"
------------------------
Medimos caracteres de TEXTO, que es lo que llega al modelo en el Camino B:
20 KB = 20.000 caracteres tal como los extrae la API
(``path_b_local.extract_pdf_with_pypdf``), con un margen de ±3 %. El
fichero PDF ocupa algo más, por las instrucciones de dibujo de cada línea.

El tamaño opcional de 400 KB pasa del límite de la API
(``MAX_ATTACHMENT_CHARS`` = 300.000 caracteres): sirve para ver que la API
falla de forma visible en vez de recortar el documento a escondidas.

Cómo se escribe un PDF a mano
-----------------------------
Igual que ``tests/doc_factories.make_text_pdf``, pero con varias páginas:
un PDF es una lista de objetos numerados (catálogo, lista de páginas,
fuente y, por cada página, su contenido y la propia página) más una tabla
final (``xref``) con la posición en bytes de cada objeto. Dos detalles que
importan para que el texto se lea bien:

* La fuente lleva ``/Encoding /WinAnsiEncoding`` y el texto va en cp1252
  (la codificación de Windows para Europa occidental). Sin eso, pypdf lee
  "aæo" donde pone "año".
* Los caracteres ``\\``, ``(`` y ``)`` tienen significado especial dentro de
  un texto de PDF y se escapan con una barra delante.

No subimos los PDF al repositorio (lo pide el enunciado): son
deterministas, así que el runner los regenera cuando le hacen falta.

Uso desde la terminal (en la carpeta estimador-cag):

    uv run python -m evals.stress.fixtures.build_pdfs
    uv run python -m evals.stress.fixtures.build_pdfs --sizes 5,20,50,100,400
"""

from __future__ import annotations

import argparse
import textwrap
from dataclasses import dataclass
from itertools import count
from pathlib import Path

from app.services.attachments.path_b_local import extract_pdf_with_pypdf

# Tamaños que pide el enunciado (0 KB = sin adjunto: no hace falta fichero).
SIZES_KB = (5, 20, 50, 100)
# Un "KB" de texto: 1.000 caracteres extraídos.
CHARS_PER_KB = 1_000
# Carpeta donde se escriben: la de este módulo (ignorada por git).
FIXTURES_DIR = Path(__file__).resolve().parent

# Maquetación: A4, letra Helvetica de 10 puntos, 55 líneas de como mucho
# 95 caracteres por página. Caben con margen en el ancho de la hoja.
_PAGE_WIDTH, _PAGE_HEIGHT = 595, 842
_LINES_PER_PAGE = 55
_LINE_WIDTH = 95
_FONT_SIZE, _LEADING = 10, 12
_LEFT, _TOP = 40, 800


@dataclass(frozen=True)
class PlantedFact:
    """Un dato escondido en el PDF que el modelo debería usar al estimar.

    * ``label``: nombre corto para el CSV.
    * ``sentence``: la frase que va en el documento.
    * ``fact``: lo que se busca en la respuesta (palabra completa, sin
      distinguir mayúsculas), con ``MemoryDriftMetric``.
    * ``position``: dónde está en el documento.
    """

    label: str
    sentence: str
    fact: str
    position: str


PLANTED_FACTS = (
    PlantedFact(
        "pdf_sap",
        "Requisito técnico adicional: la reserva de salas se integrará con SAP para imputar "
        "el coste de cada reserva al departamento que la hace.",
        "sap",
        "principio",
    ),
    PlantedFact(
        "pdf_migration",
        "Requisito técnico adicional: hay que migrar al sistema nuevo las 12.000 reservas "
        "históricas que hoy están en hojas de cálculo.",
        "12.000 reservas",
        "medio",
    ),
    PlantedFact(
        "pdf_iso",
        "Requisito técnico adicional: el proveedor deberá contar con la certificación ISO 27001 "
        "antes de la puesta en producción.",
        "iso 27001",
        "final",
    ),
)

TITLE = "Anexo técnico del proyecto · Normativa interna de uso de las salas"

# El relleno: normas de uso que no piden funciones nuevas al software.
_RULES = (
    "Las salas deben quedar ordenadas y con las luces apagadas al terminar la reunión.",
    "Quien reserva una sala es responsable de su uso durante toda la franja reservada.",
    "Las reuniones de más de diez personas se celebrarán en las salas de la planta baja.",
    "No está permitido comer en las salas, salvo en los desayunos de trabajo autorizados.",
    "El material audiovisual debe devolverse a su armario después de cada uso.",
    "Las incidencias de climatización se comunican al servicio de mantenimiento.",
    "Las visitas externas deben registrarse en recepción antes de acceder a las salas.",
    "Las salas pequeñas están pensadas para reuniones de una a cuatro personas.",
    "Se ruega puntualidad: la siguiente reserva puede empezar justo a continuación.",
    "Las pizarras deben quedar limpias para la persona que use la sala después.",
    "Los cables y adaptadores de las pantallas no deben sacarse de la sala.",
    "En caso de emergencia se seguirán las indicaciones del plan de evacuación del edificio.",
    "Las sillas de las salas no se trasladan a otras salas ni a los puestos de trabajo.",
    "Las ventanas deben quedar cerradas al salir, sobre todo en los días de lluvia.",
    "Los objetos olvidados se entregan en recepción, donde se guardan durante un mes.",
    "Las salas con videoconferencia tienen prioridad para las reuniones con otras sedes.",
    "El ruido debe mantenerse en un nivel que no moleste a los puestos cercanos.",
    "Las decoraciones temporales se retiran el mismo día en que se colocan.",
    "La sala del consejo solo se usa con autorización expresa de dirección.",
    "Las bebidas se permiten siempre que se recojan los vasos al terminar.",
)


# ---------------------------------------------------------------------------
# 1. El texto: relleno + datos plantados, ya cortado en líneas
# ---------------------------------------------------------------------------

def _wrap(paragraph: str) -> list[str]:
    return textwrap.wrap(paragraph, width=_LINE_WIDTH)


def _filler_paragraphs():
    """Artículos de normativa numerados, sin fin: "Artículo 1. ...", "Artículo 2. ..."."""
    for number in count(1):
        yield f"Artículo {number}. {_RULES[(number - 1) % len(_RULES)]}"


def _approx_extracted_chars(lines: list[str]) -> int:
    """Cuántos caracteres extraerá pypdf de estas líneas, aproximadamente.

    pypdf devuelve cada línea seguida de un salto, y la API une las páginas
    con una línea en blanco: por eso se suma 1 por línea y 1 por página.
    """
    pages = max(1, -(-len(lines) // _LINES_PER_PAGE))
    return sum(len(line) + 1 for line in lines) + pages


def build_lines(target_chars: int) -> list[str]:
    """Las líneas del documento, con el tamaño pedido y los tres datos plantados.

    Primero el título y el dato del principio; luego artículos de relleno
    hasta llenar el tamaño; el dato del medio se intercala justo a la mitad
    y el del final se añade al acabar.
    """
    start, middle, end = (_wrap(fact.sentence) for fact in PLANTED_FACTS)
    head = [TITLE, *start]
    filler: list[str] = []
    paragraphs = _filler_paragraphs()
    while _approx_extracted_chars(head + filler + middle + end) < target_chars:
        filler.extend(_wrap(next(paragraphs)))
    half = len(filler) // 2
    return head + filler[:half] + middle + filler[half:] + end


# ---------------------------------------------------------------------------
# 2. El PDF, escrito a mano
# ---------------------------------------------------------------------------

def _pdf_string(line: str) -> bytes:
    """Una línea como texto de PDF: cp1252 y con \\, ( y ) escapados."""
    escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("cp1252", errors="replace") + b")"


def lines_to_pdf(lines: list[str]) -> bytes:
    """Escribe un PDF con esas líneas, 55 por página.

    Objetos: 1 = catálogo, 2 = lista de páginas, 3 = la fuente, y después,
    por cada página, su contenido y la página que lo usa.
    """
    pages = [lines[i:i + _LINES_PER_PAGE] for i in range(0, len(lines), _LINES_PER_PAGE)] or [[]]
    first_page_object = 4
    page_numbers = [first_page_object + 2 * i + 1 for i in range(len(pages))]

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % n for n in page_numbers)
        + b"] /Count %d >>" % len(pages),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for page_lines in pages:
        # BT ... ET: un bloque de texto. Tf = letra y tamaño; TL = salto entre
        # líneas; Td = posición inicial; Tj = escribe; T* = baja una línea.
        body = b" T* ".join(_pdf_string(line) + b" Tj" for line in page_lines)
        content = b"BT /F1 %d Tf %d TL %d %d Td %s ET" % (_FONT_SIZE, _LEADING, _LEFT, _TOP, body)
        content_number = len(objects) + 1
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] " % (_PAGE_WIDTH, _PAGE_HEIGHT)
            + b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_number
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_position = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_position,
    )
    return bytes(out)


def build_pdf(size_kb: int) -> bytes:
    """El PDF de ``size_kb`` KB de texto (1 KB = 1.000 caracteres extraídos)."""
    return lines_to_pdf(build_lines(size_kb * CHARS_PER_KB))


def extracted_text(pdf: bytes) -> str:
    """El texto del PDF tal como lo saca la API en el Camino B (pypdf)."""
    text, _pages = extract_pdf_with_pypdf(pdf)
    return text


# ---------------------------------------------------------------------------
# 3. Escribirlos en disco (lo usa el runner)
# ---------------------------------------------------------------------------

def fixture_path(size_kb: int, out_dir: Path = FIXTURES_DIR) -> Path:
    return out_dir / f"attach_{size_kb}kb.pdf"


def ensure_fixtures(sizes_kb: tuple[int, ...] | list[int] = SIZES_KB, out_dir: Path = FIXTURES_DIR) -> dict[int, Path]:
    """Deja en ``out_dir`` un PDF por tamaño y devuelve sus rutas.

    Solo escribe los que faltan o han cambiado (por ejemplo, si alguien
    tocó este módulo). El tamaño 0 significa "sin adjunto" y se ignora.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[int, Path] = {}
    for size_kb in sizes_kb:
        if size_kb <= 0:
            continue
        path = fixture_path(size_kb, out_dir)
        pdf = build_pdf(size_kb)
        if not path.is_file() or path.read_bytes() != pdf:
            path.write_bytes(pdf)
        paths[size_kb] = path
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera los PDF sintéticos del stress test.")
    parser.add_argument("--sizes", default=",".join(str(s) for s in SIZES_KB),
                        help="Tamaños en KB de texto, separados por comas (p. ej. 5,20,50,100,400).")
    parser.add_argument("--out", type=Path, default=FIXTURES_DIR, help="Carpeta de salida.")
    args = parser.parse_args(argv)
    sizes = [int(size) for size in args.sizes.split(",") if size.strip()]

    print(f"{'Tamaño':>8} {'Caracteres':>11} {'Páginas':>8} {'Fichero':>10}  Ruta")
    for size_kb, path in ensure_fixtures(sizes, args.out).items():
        pdf = path.read_bytes()
        text, pages = extract_pdf_with_pypdf(pdf)
        print(f"{size_kb:>6} KB {len(text):>11,} {pages:>8} {len(pdf) / 1024:>7.0f} KB  {path}".replace(",", "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
