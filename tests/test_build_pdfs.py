"""Tests del generador de PDF del stress test (sesión 6, ``evals/stress/fixtures/build_pdfs.py``).

Comprueban que los PDF tienen el tamaño pedido, que los tres datos plantados
están y se leen bien (con sus tildes), que salen siempre iguales y que la
API los acepta. El de 400 KB comprueba lo contrario: que la API lo rechaza
con un error claro en vez de recortarlo a escondidas.

Se ejecutan con:

    uv run python -m pytest tests/test_build_pdfs.py -v
"""

import re

import pytest

from app.services.attachments.common import Attachment, AttachmentError, validate_attachment
from app.services.attachments.path_b_local import prepare_user_content
from evals.stress.fixtures.build_pdfs import (
    PLANTED_FACTS,
    SIZES_KB,
    build_pdf,
    ensure_fixtures,
    extracted_text,
    lines_to_pdf,
)


def one_line(text: str) -> str:
    """El texto con los saltos de línea convertidos en espacios (las frases se cortan en líneas)."""
    return " ".join(text.split())


@pytest.mark.parametrize("size_kb", SIZES_KB)
def test_each_pdf_has_the_requested_amount_of_text(size_kb):
    """"20 KB" = 20.000 caracteres tal como los extrae la API, con un margen del 3 %."""
    characters = len(extracted_text(build_pdf(size_kb)))
    assert abs(characters - size_kb * 1_000) <= 0.03 * size_kb * 1_000


def test_planted_facts_are_readable_and_at_the_start_middle_and_end():
    text = one_line(extracted_text(build_pdf(50)))
    where = {"principio": (0.0, 0.1), "medio": (0.3, 0.7), "final": (0.9, 1.0)}
    for fact in PLANTED_FACTS:
        position = text.find(fact.sentence)
        assert position >= 0, f"no se encuentra la frase de {fact.label} (¿tildes mal leídas?)"
        low, high = where[fact.position]
        assert low <= position / len(text) <= high, f"{fact.label} no está al {fact.position}"
        # Lo que buscará el runner en la respuesta está en la propia frase.
        assert re.search(rf"(?<!\w){re.escape(fact.fact)}(?!\w)", fact.sentence, re.IGNORECASE)
    assert "históricas" in text and "certificación" in text and "Artículo 1." in text


def test_pdfs_are_identical_every_time_and_the_api_accepts_them():
    assert build_pdf(20) == build_pdf(20)
    pdf = build_pdf(100)
    attachment = validate_attachment("attach_100kb.pdf", pdf, max_bytes=10 * 1_048_576)
    assert attachment.kind == "pdf"


def test_special_pdf_characters_and_accents_survive():
    line = "Precio (sin IVA): 30.000 € \\ año, señal, ¿pregunta? ¡sí!"
    assert extracted_text(lines_to_pdf([line])) == line


def test_ensure_fixtures_writes_the_files_and_rewrites_them_if_they_change(tmp_path):
    paths = ensure_fixtures((0, 5), tmp_path)  # 0 KB = sin adjunto: no hay fichero
    assert list(paths) == [5]
    assert paths[5].name == "attach_5kb.pdf"
    assert paths[5].read_bytes() == build_pdf(5)

    paths[5].write_bytes(b"estropeado")
    ensure_fixtures((5,), tmp_path)
    assert paths[5].read_bytes() == build_pdf(5)


def test_400kb_goes_over_the_api_limit_and_fails_loudly():
    """Pasarse de MAX_ATTACHMENT_CHARS (300.000) da un error 413, nunca un recorte silencioso."""
    pdf = build_pdf(400)
    attachment = Attachment(filename="attach_400kb.pdf", kind="pdf", data=pdf)
    with pytest.raises(AttachmentError) as error:
        prepare_user_content("Estimad el proyecto.", [attachment], "pypdf", max_chars=300_000)
    assert error.value.status_code == 413
