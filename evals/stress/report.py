"""Las tablas del informe del stress test (sesión 6): de ``results.csv`` a ``REPORT.md``.

Qué hace
--------
Lee el CSV del runner y calcula las tablas que pide el enunciado:

1. Tabla resumen: una fila por escenario × tamaño de PDF, con la latencia
   típica (P50) y la del peor caso habitual (P95), el coste medio de una
   conversación, el uso de caché y cuánto se recuerda.
2. Las tres "curvas", escritas como tablas (sin gráficos):
   * latencia frente a tokens de entrada, por tramos;
   * coste acumulado frente al número de turno;
   * recuerdo de la hoja de corrección frente a N (turnos 1, 3, 6, 10, 20).
3. Una tabla más: los tres datos del PDF en la respuesta del turno 1, por
   tamaño y por posición en el documento (principio, medio, final). Es la
   que dice si hay "lost in the middle".

Las tablas se colocan en ``REPORT.md`` entre dos marcas invisibles
(``<!-- tablas:inicio -->`` y ``<!-- tablas:fin -->``). Lo que está fuera de
las marcas (la explicación y los dos párrafos de lectura) lo escribimos a
mano y no se toca: si se repite la batería, basta con volver a lanzar este
módulo. Las conclusiones no las saca un programa.

Uso, desde la carpeta estimador-cag:

    uv run python -m evals.stress.report
    uv run python -m evals.stress.report --csv otra/carpeta/results.csv --report otro/REPORT.md

Qué es P50 y P95: ordenadas todas las latencias, P50 es la del medio (la
mitad de los turnos tardan menos) y P95 la que solo superan 5 de cada 100
turnos: el "peor caso habitual", lo que sufre el cliente en un mal momento.
"""

from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from evals.stress.fixtures.build_pdfs import PLANTED_FACTS
from evals.stress.scenarios import CHECKPOINTS, SCENARIOS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = PROJECT_ROOT / "evals" / "stress" / "results.csv"
DEFAULT_REPORT = PROJECT_ROOT / "evals" / "stress" / "REPORT.md"

START_MARK = "<!-- tablas:inicio · las genera evals/stress/report.py: no editar a mano -->"
END_MARK = "<!-- tablas:fin -->"

SCENARIO_NAMES = {"growing": "Crece", "pivot": "Cambio de rumbo", "contradiction": "Contradicción"}
# Tramos de tokens de entrada para la curva de latencia.
TOKEN_BINS = (0, 5_000, 10_000, 20_000, 40_000, 80_000)

Row = dict[str, str]


# ---------------------------------------------------------------------------
# 1. Utilidades: leer, calcular y escribir números en castellano
# ---------------------------------------------------------------------------

def load_rows(csv_path: Path) -> list[Row]:
    with csv_path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _num(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


def percentile(values: Iterable[float], p: int) -> float | None:
    """El percentil p (de 1 a 99) de una lista de números; None si está vacía.

    Usa ``statistics.quantiles`` (biblioteca estándar, sin numpy) con el
    método "inclusive": con pocos datos da valores dentro del rango medido.
    Con un solo valor, el percentil es ese valor.
    """
    data = sorted(values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    return statistics.quantiles(data, n=100, method="inclusive")[p - 1]


def _mean(values: Iterable[float]) -> float | None:
    data = list(values)
    return statistics.fmean(data) if data else None


def fmt(value: float | None, decimals: int = 1, suffix: str = "") -> str:
    """1234.5 -> "1.234,5" (punto de millares, coma decimal). None -> "—"."""
    if value is None:
        return "—"
    text = f"{value:,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{text}{suffix}"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{fmt(value * 100, 0)} %"


def _table(headers: list[str], rows: list[list[str]], align_right_from: int = 1) -> str:
    """Una tabla Markdown. Las columnas desde ``align_right_from`` van a la derecha."""
    aligns = ["---" if i < align_right_from else "---:" for i in range(len(headers))]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(aligns) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _seconds(rows: Iterable[Row], column: str) -> list[float]:
    return [float(r[column]) / 1000 for r in rows if r.get(column) not in (None, "")]


def _done(rows: Iterable[Row]) -> list[Row]:
    return [r for r in rows if r["outcome"] == "done"]


def _sizes(rows: list[Row]) -> list[int]:
    return sorted({int(r["attachment_kb"]) for r in rows})


def _scenarios(rows: list[Row]) -> list[str]:
    present = {r["scenario"] for r in rows}
    return [name for name in SCENARIOS if name in present]


def _conversation_costs(rows: list[Row]) -> list[float]:
    """Coste total de cada conversación (el coste acumulado de su último turno)."""
    last: dict[str, Row] = {}
    for row in rows:
        if row["session_id"] not in last or int(row["turn_index"]) > int(last[row["session_id"]]["turn_index"]):
            last[row["session_id"]] = row
    return [float(r["cumulative_cost_usd"]) for r in last.values() if r["cumulative_cost_usd"]]


# ---------------------------------------------------------------------------
# 2. Las tablas
# ---------------------------------------------------------------------------

def summary_table(rows: list[Row]) -> str:
    """Una fila por escenario × tamaño de PDF."""
    table = []
    for scenario in _scenarios(rows):
        for size in _sizes(rows):
            group = [r for r in rows if r["scenario"] == scenario and int(r["attachment_kb"]) == size]
            if not group:
                continue
            done = _done(group)
            latencies = _seconds(done, "latency_ms")
            kinds = [r["cache_hit_kind"] for r in done]
            drift = [float(r["memory_drift_score"]) for r in done if r["memory_drift_score"]]
            first_pdf = [float(r["attachment_recall_score"]) for r in done
                         if r["turn_index"] == "1" and r["attachment_recall_score"]]
            table.append([
                SCENARIO_NAMES.get(scenario, scenario),
                "sin PDF" if size == 0 else f"{size} KB",
                fmt(len(group), 0),
                fmt(len(group) - len(done), 0),
                fmt(percentile(latencies, 50), 1, " s"),
                fmt(percentile(latencies, 95), 1, " s"),
                fmt(_mean(_conversation_costs(group)), 4, " $"),
                pct(kinds.count("exact") / len(kinds) if kinds else None),
                pct(kinds.count("semantic") / len(kinds) if kinds else None),
                pct(_mean(drift)),
                "—" if size == 0 else pct(_mean(first_pdf)),
            ])
    return _table(
        ["Escenario", "PDF", "Turnos", "Con error", "Latencia P50", "Latencia P95",
         "Coste medio por conversación", "Caché exacta", "Caché semántica",
         "Recuerdo medio (hoja de corrección)", "Datos del PDF en el turno 1"],
        table,
        align_right_from=2,
    )


def latency_vs_tokens_table(rows: list[Row]) -> str:
    """Curva 1: cuánto tarda según lo que le mandamos (tokens de entrada)."""
    done = [r for r in _done(rows) if r["tokens_in"]]
    table = []
    edges = list(TOKEN_BINS) + [None]
    for low, high in zip(edges[:-1], edges[1:]):
        group = [r for r in done if float(r["tokens_in"]) >= low and (high is None or float(r["tokens_in"]) < high)]
        if not group:
            continue
        label = f"{fmt(low, 0)} – {fmt(high, 0)}" if high else f"{fmt(low, 0)} o más"
        table.append([
            label,
            fmt(len(group), 0),
            fmt(percentile(_seconds(group, "ttft_ms"), 50), 1, " s"),
            fmt(percentile(_seconds(group, "latency_ms"), 50), 1, " s"),
            fmt(percentile(_seconds(group, "latency_ms"), 95), 1, " s"),
            fmt(_mean(float(r["tokens_out"]) for r in group if r["tokens_out"]), 0),
        ])
    return _table(
        ["Tokens de entrada", "Turnos", "Primer texto P50", "Latencia P50", "Latencia P95",
         "Tokens de salida (media)"],
        table,
    )


def cumulative_cost_table(rows: list[Row]) -> str:
    """Curva 2: lo que lleva gastado la conversación en cada turno (media de repeticiones).

    Una columna por escenario sin PDF y con el PDF más grande: así se ve la
    diferencia que hace el documento y el escalón cuando sale de la ventana.
    """
    sizes = _sizes(rows)
    pairs = [(s, size) for s in _scenarios(rows) for size in sorted({sizes[0], sizes[-1]})]
    by_key: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    for r in rows:
        if r["cumulative_cost_usd"]:
            by_key[(r["scenario"], int(r["attachment_kb"]), int(r["turn_index"]))].append(float(r["cumulative_cost_usd"]))
    turns = sorted({int(r["turn_index"]) for r in rows})
    table = [
        [str(turn)] + [fmt(_mean(by_key.get((s, size, turn), [])), 4) for s, size in pairs]
        for turn in turns
    ]
    headers = ["Turno"] + [
        f"{SCENARIO_NAMES.get(s, s)} · {'sin PDF' if size == 0 else f'{size} KB'} ($)" for s, size in pairs
    ]
    return _table(headers, table)


def drift_vs_n_table(rows: list[Row]) -> str:
    """Curva 3: qué parte de lo que debía recordar sigue viva tras el turno N."""
    scenarios = _scenarios(rows)
    table = []
    for n in CHECKPOINTS:
        cells = [str(n)]
        for scenario in scenarios:
            group = [r for r in _done(rows)
                     if r["scenario"] == scenario and int(r["turn_index"]) == n and r["memory_drift_score"]]
            if not group:
                cells.append("—")
                continue
            checked = round(_mean(int(r["memory_drift_checked"]) for r in group))
            cells.append(f"{pct(_mean(float(r['memory_drift_score']) for r in group))} ({checked} datos)")
        table.append(cells)
    return _table(["Turno N"] + [SCENARIO_NAMES.get(s, s) for s in scenarios], table)


def pdf_recall_table(rows: list[Row]) -> str:
    """Los tres datos del PDF en la respuesta del turno 1, por tamaño y posición."""
    first = [r for r in _done(rows) if r["turn_index"] == "1" and r["attachment_recall_details"]]
    table = []
    for size in [s for s in _sizes(rows) if s > 0]:
        group = [r for r in first if int(r["attachment_kb"]) == size]
        if not group:
            continue
        cells = [f"{size} KB", fmt(len(group), 0)]
        for fact in PLANTED_FACTS:
            hits = [f"{fact.label}=1" in r["attachment_recall_details"].split(";") for r in group]
            cells.append(pct(sum(hits) / len(hits)))
        table.append(cells)
    return _table(
        ["PDF", "Conversaciones"] + [f"«{f.fact}» ({f.position})" for f in PLANTED_FACTS],
        table,
    )


def render_tables(rows: list[Row]) -> str:
    """Todas las tablas, con una línea que dice de dónde salen los datos."""
    models = sorted({r["model"] for r in rows if r.get("model")})
    run_ids = sorted({r["run_id"] for r in rows})
    errors = len(rows) - len(_done(rows))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        f"_Datos: {fmt(len(rows), 0)} turnos ({fmt(errors, 0)} con error) · modelo "
        f"{', '.join(models) or '—'} · ejecución {', '.join(run_ids)} · tablas generadas el {generated}._",
        "### 1. Resumen por escenario y tamaño de PDF",
        summary_table(rows),
        "_No hay caché de respuestas en este proyecto: las dos columnas de caché valen 0 % por diseño._",
        "### 2. Curva 1 · Latencia frente a tokens de entrada",
        latency_vs_tokens_table(rows),
        "### 3. Curva 2 · Coste acumulado por turno (media de las repeticiones)",
        cumulative_cost_table(rows),
        "### 4. Curva 3 · Recuerdo de la hoja de corrección tras el turno N",
        drift_vs_n_table(rows),
        "_Media de las comprobaciones de ese turno (datos dichos antes de N). En el turno 1 no hay "
        "nada anterior que comprobar._",
        "### 5. Datos del PDF en la respuesta del turno 1, por posición en el documento",
        pdf_recall_table(rows),
    ]
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 3. Colocar las tablas en REPORT.md sin tocar el texto escrito a mano
# ---------------------------------------------------------------------------

REPORT_TEMPLATE = f"""# Stress test del CAG · ¿dónde empieza a romperse?

Sesión 6 del Máster LiDR AI Engineering · proyecto `estimador-cag`.

## Qué se ha medido

_Pendiente: se escribe con la batería real (Paso 07)._

## Resultados

{START_MARK}
{END_MARK}

## Lectura: dónde empieza a romperse este CAG y por qué

_Pendiente: dos párrafos con cifras concretas, escritos con la batería real (Paso 07)._
"""


def update_report(report_path: Path, tables_md: str) -> Path:
    """Pone las tablas entre las marcas de REPORT.md (lo crea si no existe)."""
    text = report_path.read_text(encoding="utf-8") if report_path.is_file() else REPORT_TEMPLATE
    if START_MARK not in text or END_MARK not in text:
        raise ValueError(f"{report_path} no tiene las marcas de las tablas; añade:\n{START_MARK}\n{END_MARK}")
    before, rest = text.split(START_MARK, 1)
    _old, after = rest.split(END_MARK, 1)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(f"{before}{START_MARK}\n\n{tables_md}\n\n{END_MARK}{after}", encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera las tablas de REPORT.md a partir de results.csv.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    rows = load_rows(args.csv)
    if not rows:
        print(f"{args.csv} está vacío: lanza antes la batería (python -m evals.stress.run).")
        return 1
    print(f"Informe actualizado: {update_report(args.report, render_tables(rows))} ({len(rows)} turnos)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
