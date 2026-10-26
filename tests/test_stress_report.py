"""Tests del informe del stress test (sesión 6, ``evals/stress/report.py``).

Comprueban los cálculos (percentiles, números en castellano), que salen las
cinco tablas con datos de una batería del modelo falso, y que al regenerar
las tablas el texto escrito a mano en REPORT.md no se toca.

Se ejecutan con:

    uv run python -m pytest tests/test_stress_report.py -v
"""

import pytest

from evals.stress.report import (
    END_MARK,
    START_MARK,
    fmt,
    load_rows,
    percentile,
    render_tables,
    update_report,
)
from evals.stress.run import RunConfig, main_async


def test_percentile_with_none_one_and_several_values():
    assert percentile([], 50) is None
    assert percentile([7.0], 95) == 7.0
    assert percentile([4.0, 1.0, 3.0, 2.0], 50) == pytest.approx(2.5)
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == pytest.approx(3.85)


def test_numbers_are_written_the_spanish_way():
    assert fmt(1234.5) == "1.234,5"
    assert fmt(0.01234, 4, " $") == "0,0123 $"
    assert fmt(None) == "—"


@pytest.fixture
async def fake_rows(tmp_path):
    config = RunConfig(
        scenarios=["contradiction"], sizes_kb=[0, 5], repeats=1, turns=14, fake_llm=True,
        retry_wait_s=(0.0,), output=tmp_path / "results.csv", fixtures_dir=tmp_path,
    )
    await main_async(config)
    return load_rows(config.output)


async def test_render_tables_has_the_summary_the_three_curves_and_the_pdf_table(fake_rows):
    tables = render_tables(fake_rows)

    for title in ("### 1. Resumen", "### 2. Curva 1", "### 3. Curva 2", "### 4. Curva 3", "### 5. Datos del PDF"):
        assert title in tables
    assert "| Contradicción | sin PDF | 14 | 0 |" in tables
    assert "| Contradicción | 5 KB | 14 | 0 |" in tables
    # Curva 2: una fila por turno, con columnas sin PDF y con el PDF más grande.
    assert "| 14 | " in tables and "Contradicción · 5 KB ($)" in tables
    # Curva 3: los turnos de control (sin el 20, porque esta batería tiene 14 turnos).
    assert "| 10 | " in tables and "| 20 | — |" in tables
    # Tabla 5: el modelo falso usa los tres datos del PDF en el turno 1.
    assert "| 5 KB | 1 | 100 % | 100 % | 100 % |" in tables


async def test_update_report_keeps_the_handwritten_text(tmp_path, fake_rows):
    report = tmp_path / "REPORT.md"
    update_report(report, "tablas de la primera vez")
    assert START_MARK in report.read_text(encoding="utf-8")

    # Escribimos la lectura a mano y regeneramos las tablas.
    text = report.read_text(encoding="utf-8").replace(
        "_Pendiente: dos párrafos con cifras concretas, escritos con la batería real (Paso 07)._",
        "A partir del turno 9 el presupuesto ya no está en ningún sitio.",
    )
    report.write_text(text, encoding="utf-8")
    update_report(report, render_tables(fake_rows))

    final = report.read_text(encoding="utf-8")
    assert "A partir del turno 9 el presupuesto ya no está en ningún sitio." in final
    assert "tablas de la primera vez" not in final
    assert final.count(START_MARK) == 1 and final.count(END_MARK) == 1


def test_update_report_refuses_a_report_without_marks(tmp_path):
    report = tmp_path / "REPORT.md"
    report.write_text("# Informe sin marcas\n", encoding="utf-8")
    with pytest.raises(ValueError):
        update_report(report, "tablas")
