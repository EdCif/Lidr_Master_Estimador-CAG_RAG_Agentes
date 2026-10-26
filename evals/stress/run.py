"""Runner del stress test (sesión 6): lanza las conversaciones y escribe el CSV.

Qué hace
--------
Hace de cliente "misterioso" automático. Para cada combinación de escenario
(``scenarios.py``) × tamaño de adjunto (``fixtures/build_pdfs.py``) ×
repetición:

1. Crea una sesión nueva (``POST /sessions``).
2. Manda los turnos del guion uno a uno (``POST /sessions/{id}/estimate``),
   con el PDF en el primer turno, igual que un cliente en Streamlit.
3. Tras cada turno:
   * recoge la ficha técnica del turno (``turn_observed``) del evento final;
   * pide la foto de la sesión (``GET /sessions/{id}``): memoria, historial
     y última respuesta;
   * pasa las métricas (``metrics.py``): presupuesto de latencia y de coste,
     deriva de memoria con la hoja de corrección del guion, y recall de los
     tres datos plantados en el PDF;
   * escribe una fila en el CSV.

Por qué lee ``turn_observed`` del evento ``done`` y no de la consola
--------------------------------------------------------------------
El enunciado deja elegir entre leer las líneas ``turn_observed`` de la
consola de la API o pedirlas a la API. La consola obligaría a ser dueños
del proceso de la API y, con varias conversaciones a la vez, a emparejar
cada línea con su sesión. En la respuesta, cada observación llega con su
propia petición: no hay nada que emparejar. La línea de consola sigue
existiendo para quien mire la API en vivo.

Tres modos
----------
* ``--fake-llm``: la API en memoria con un modelo de mentira
  (``fake_llm.py``). Gratis y en segundos: para comprobar que todo encaja.
* ``--http URL``: contra una API ya arrancada (``arrancar_todo.bat``). Es el
  modo de la batería real. El modelo lo decide la API al arrancar
  (``SESSION_LLM_MODEL``).
* ``--in-process``: la API en memoria con el modelo real (necesita el .env).

Uso, desde la carpeta estimador-cag:

    # Comprobar la fontanería, gratis:
    uv run python -m evals.stress.run --fake-llm --repeats 1

    # La batería del enunciado, contra la API en marcha:
    uv run python -m evals.stress.run --http http://127.0.0.1:8001 \\
        --scenarios growing,pivot,contradiction --attachment-sizes 0,5,20,50,100 \\
        --repeats 3 --output evals/stress/results.csv

Robustez: el CSV se escribe y se guarda fila a fila (si la batería se corta,
lo hecho no se pierde) y los fallos pasajeros del proveedor se reintentan.
Un turno fallido no toca el historial de la sesión, así que repetirlo es
seguro.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import statistics
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from app.services.turn_observer import REQUIRED_FIELDS, TURN_OBSERVED
from evals.stress.fixtures.build_pdfs import FIXTURES_DIR, PLANTED_FACTS, ensure_fixtures
from evals.stress.metrics import (
    DEFAULT_COST_BUDGET_USD,
    DEFAULT_LATENCY_BUDGET_MS,
    CostBudgetMetric,
    LatencyBudgetMetric,
    MemoryDriftMetric,
    MetricResult,
)
from evals.stress.scenarios import TURNS_PER_SCENARIO, Scenario, get_scenarios
from evals.stress.sse import parse_sse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "evals" / "stress" / "results.csv"

# Columnas del CSV, en orden. Una fila por turno (también los que fallan).
_RUN_COLUMNS = [
    "run_id", "started_at", "scenario", "attachment_kb", "repeat", "turn_index",
    "session_id", "http_status", "outcome", "attempts", "error_detail",
]
# Los 13 campos del enunciado (turn_index y session_id ya están arriba).
_OBSERVED_COLUMNS = [f for f in REQUIRED_FIELDS if f not in ("turn_index", "session_id")]
# Los extras de la observación que ayudan a leer las curvas.
_EXTRA_COLUMNS = [
    "tokens_source", "model", "attachment_mode", "attachments_count", "ttft_ms", "llm_ms",
    "reply_chars", "metadata_chars", "turns_dropped", "messages_sent",
]
# Lo que añade el runner: su propio reloj, el coste acumulado y las métricas.
_RUNNER_COLUMNS = [
    "client_latency_ms", "cumulative_cost_usd",
    "latency_budget_passed", "cost_budget_passed",
    "memory_drift_score", "memory_drift_passed", "memory_drift_checked", "memory_drift_details",
    "attachment_recall_score", "attachment_recall_details",
]
FIELDNAMES = _RUN_COLUMNS + _OBSERVED_COLUMNS + _EXTRA_COLUMNS + _RUNNER_COLUMNS

# Esperas largas a propósito: un turno real puede tardar más de un minuto.
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=60.0)


@dataclass
class RunConfig:
    """Todo lo que decide una ejecución del runner (sale de la línea de comandos)."""

    scenarios: list[str]
    sizes_kb: list[int]
    repeats: int = 1
    turns: int = TURNS_PER_SCENARIO
    http: str | None = None
    in_process: bool = False
    fake_llm: bool = False
    model: str | None = None
    concurrency: int = 2
    retries: int = 2
    retry_wait_s: tuple[float, ...] = (5.0, 15.0)
    budget_ms: int = DEFAULT_LATENCY_BUDGET_MS
    budget_usd: float = DEFAULT_COST_BUDGET_USD
    output: Path = DEFAULT_OUTPUT
    fixtures_dir: Path = FIXTURES_DIR


@dataclass
class TurnResult:
    """Lo que ha pasado con un turno, visto desde el cliente."""

    http_status: int
    final_event: str  # "done", "error" o "http_error"
    final_data: dict[str, Any]
    client_ms: int
    attempts: int


# ---------------------------------------------------------------------------
# 1. Hablar con la API
# ---------------------------------------------------------------------------

def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))
    except ValueError:
        return response.text[:300]


async def post_turn(
    client: httpx.AsyncClient,
    session_id: str,
    transcript: str,
    attachment: tuple[str, bytes] | None,
    config: RunConfig,
) -> TurnResult:
    """Manda un turno y espera su evento final, reintentando los fallos pasajeros.

    Se reintenta solo lo que puede salir bien a la segunda: un evento
    ``error`` marcado como ``retryable`` (el proveedor ha fallado: caído,
    límite de peticiones...) o un corte de la conexión. Lo demás (un PDF sin
    texto, un adjunto demasiado grande) fallaría igual y se apunta tal cual.
    """
    attempts = 0
    while True:
        attempts += 1
        files = {"attachments": (attachment[0], attachment[1], "application/pdf")} if attachment else None
        started = time.perf_counter()
        try:
            response = await client.post(
                f"/sessions/{session_id}/estimate",
                data={"transcript": transcript, "attachment_mode": "local"},
                files=files,
            )
        except httpx.TransportError as error:
            outcome = TurnResult(0, "http_error", {"detail": f"Sin conexión con la API: {error!r}"}, 0, attempts)
            retryable = True
        else:
            client_ms = round((time.perf_counter() - started) * 1000)
            if response.status_code != 200:
                return TurnResult(response.status_code, "http_error", {"detail": _detail(response)}, client_ms, attempts)
            events = parse_sse(response.text)
            name, data = events[-1] if events else ("error", {"detail": "La respuesta llegó vacía."})
            if name not in ("done", "error"):
                name, data = "error", {"detail": "La respuesta se cortó antes del evento final."}
            outcome = TurnResult(200, name, data, client_ms, attempts)
            retryable = name == "error" and bool(data.get("retryable"))

        if not retryable or attempts > config.retries:
            return outcome
        await asyncio.sleep(config.retry_wait_s[min(attempts - 1, len(config.retry_wait_s) - 1)])


async def get_snapshot(client: httpx.AsyncClient, session_id: str) -> dict[str, Any] | None:
    """La foto de la sesión (memoria, historial, última observación), o None."""
    try:
        response = await client.get(f"/sessions/{session_id}")
    except httpx.TransportError:
        return None
    return response.json() if response.status_code == 200 else None


# ---------------------------------------------------------------------------
# 2. Evaluar un turno
# ---------------------------------------------------------------------------

def _summary(results: list[tuple[str, MetricResult]]) -> dict[str, Any]:
    """Resume varias comprobaciones: media, si pasan todas, cuántas y el detalle."""
    if not results:
        return {"score": "", "passed": "", "checked": 0, "details": ""}
    return {
        "score": round(statistics.fmean(result.score for _, result in results), 4),
        "passed": int(all(result.passed for _, result in results)),
        "checked": len(results),
        "details": ";".join(f"{label}={int(result.passed)}" for label, result in results),
    }


def evaluate_turn(
    observation: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    scenario: Scenario,
    turn_index: int,
    attachment_kb: int,
    config: RunConfig,
) -> dict[str, Any]:
    """Las columnas de métricas de un turno.

    * Presupuestos: sobre la observación del turno.
    * Deriva de memoria: los hechos de la hoja de corrección plantados en
      turnos anteriores (``scenario.facts_to_check``), sobre la foto.
    * Recall del adjunto: los tres datos del PDF, buscados SOLO en la última
      respuesta (en el historial estarían siempre, dentro del documento).
    """
    columns: dict[str, Any] = {}
    if observation:
        columns["latency_budget_passed"] = int(LatencyBudgetMetric(config.budget_ms).evaluate(observation).passed)
        columns["cost_budget_passed"] = int(CostBudgetMetric(config.budget_usd).evaluate(observation).passed)
    if snapshot:
        drift = _summary([
            (fact.label, fact.metric().evaluate(snapshot)) for _, fact in scenario.facts_to_check(turn_index)
        ])
        columns.update({
            "memory_drift_score": drift["score"],
            "memory_drift_passed": drift["passed"],
            "memory_drift_checked": drift["checked"],
            "memory_drift_details": drift["details"],
        })
        if attachment_kb > 0:
            recall = _summary([
                (fact.label, MemoryDriftMetric(fact.fact, where=("reply",)).evaluate(snapshot))
                for fact in PLANTED_FACTS
            ])
            columns["attachment_recall_score"] = recall["score"]
            columns["attachment_recall_details"] = recall["details"]
    return columns


# ---------------------------------------------------------------------------
# 3. Una conversación completa y el CSV
# ---------------------------------------------------------------------------

class CsvSink:
    """Escribe las filas del CSV y las guarda en disco una a una."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=FIELDNAMES, extrasaction="ignore")
        self._writer.writeheader()
        self.rows = 0

    def write(self, row: dict[str, Any]) -> None:
        self._writer.writerow({key: ("" if value is None else value) for key, value in row.items()})
        self._file.flush()
        self.rows += 1

    def close(self) -> None:
        self._file.close()


@dataclass
class SessionSummary:
    """Lo que se cuenta por pantalla al terminar cada conversación."""

    scenario: str
    attachment_kb: int
    repeat: int
    turns: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


async def run_session(
    client: httpx.AsyncClient,
    scenario: Scenario,
    attachment_kb: int,
    repeat: int,
    pdf: bytes | None,
    config: RunConfig,
    sink: CsvSink,
    run_id: str,
) -> SessionSummary:
    """Una conversación: sesión nueva, los turnos del guion y una fila por turno."""
    summary = SessionSummary(scenario.name, attachment_kb, repeat)
    started = time.perf_counter()
    response = await client.post("/sessions")
    session_id = response.json()["session_id"]

    for turn in scenario.turns[: config.turns]:
        attachment = (f"attach_{attachment_kb}kb.pdf", pdf) if (turn.turn_index == 1 and pdf) else None
        result = await post_turn(client, session_id, turn.transcript, attachment, config)
        observation = result.final_data.get("turn_observed")
        snapshot = await get_snapshot(client, session_id)

        cost = (observation or {}).get("cost_usd")
        summary.cost_usd += cost or 0.0
        row: dict[str, Any] = {
            "run_id": run_id,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scenario": scenario.name,
            "attachment_kb": attachment_kb,
            "repeat": repeat,
            "turn_index": turn.turn_index,
            "session_id": session_id,
            "http_status": result.http_status,
            "outcome": result.final_event,
            "attempts": result.attempts,
            "error_detail": None if result.final_event == "done" else result.final_data.get("detail"),
            "client_latency_ms": result.client_ms,
            "cumulative_cost_usd": round(summary.cost_usd, 6),
        }
        for column in _OBSERVED_COLUMNS + _EXTRA_COLUMNS:
            row[column] = (observation or {}).get(column)
        row.update(evaluate_turn(observation, snapshot, scenario, turn.turn_index, attachment_kb, config))
        sink.write(row)

        summary.turns += 1
        if result.final_event != "done":
            summary.errors += 1
            # Sin sesión (404/410/sin conexión) o sin el adjunto del turno 1
            # (por ejemplo, el PDF de 400 KB pasa del límite) no tiene sentido
            # seguir: el resto de la conversación ya no mediría lo mismo.
            if result.final_event == "http_error" or (turn.turn_index == 1 and attachment):
                summary.notes.append(f"se detiene en el turno {turn.turn_index}: {row['error_detail']}")
                break

    summary.seconds = time.perf_counter() - started
    return summary


# ---------------------------------------------------------------------------
# 4. Preparar el cliente (los tres modos) y lanzar la batería
# ---------------------------------------------------------------------------

def make_app(settings: Any):
    """La API de sesiones en memoria, como en tests/conftest.py, con estos ajustes."""
    from fastapi import FastAPI

    from app.config import get_settings
    from app.errors import install_error_handlers
    from app.routers.sessions import get_session_store, router
    from app.services.prompt_builder import build_session_system_prompt
    from app.services.sessions import SessionStore

    store = SessionStore(
        system_prompt_builder=build_session_system_prompt,
        max_turns=settings.max_turns,
        max_sessions=settings.max_sessions,
        ttl_seconds=settings.session_ttl_seconds,
    )
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_session_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def _in_process_settings(config: RunConfig) -> Any:
    from app.config import Settings, get_settings

    if config.fake_llm:
        # Ajustes de verdad pero sin leer el .env: claves de mentira, que el
        # modelo falso no usa. Así funciona también en la CI.
        return Settings(
            _env_file=None,
            openai_api_key="fake-key-no-real-credential",
            anthropic_api_key="fake-key-no-real-credential",
            session_llm_model=config.model or "claude-haiku-5-5",
        )
    settings = get_settings()
    return settings.model_copy(update={"session_llm_model": config.model}) if config.model else settings


async def main_async(config: RunConfig) -> int:
    scenarios = get_scenarios(config.scenarios)
    pdf_paths = ensure_fixtures([size for size in config.sizes_kb if size > 0], config.fixtures_dir)
    pdfs = {size: path.read_bytes() for size, path in pdf_paths.items()}
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with ExitStack() as stack:
        if config.http:
            client = httpx.AsyncClient(base_url=config.http.rstrip("/"), timeout=_HTTP_TIMEOUT)
            try:
                (await client.get("/health")).raise_for_status()
            except httpx.HTTPError:
                print(f"No hay ninguna API respondiendo en {config.http}. Arráncala con arrancar_todo.bat.")
                await client.aclose()
                return 1
            mode = f"http · {config.http} (el modelo lo decide la API)"
        else:
            settings = _in_process_settings(config)
            if config.fake_llm:
                from app.services import conversation_service
                from evals.stress.fake_llm import FakeStressLLM

                stack.enter_context(patch.object(
                    conversation_service.path_b_local, "stream_reply", FakeStressLLM().stream_reply
                ))
            # En memoria, la API escribiría una línea JSON por turno en esta
            # misma consola: la callamos para que se lea el progreso.
            observed_logger = logging.getLogger(TURN_OBSERVED)
            stack.callback(observed_logger.setLevel, observed_logger.level)
            observed_logger.setLevel(logging.WARNING)
            transport = httpx.ASGITransport(app=make_app(settings))
            client = httpx.AsyncClient(transport=transport, base_url="http://stress-test", timeout=_HTTP_TIMEOUT)
            mode = ("en memoria · modelo de prueba" if config.fake_llm
                    else f"en memoria · {settings.session_llm_model}")

        combos = [
            (scenario, size, repeat)
            for scenario in scenarios
            for size in config.sizes_kb
            for repeat in range(1, config.repeats + 1)
        ]
        print(f"Stress test · {mode}")
        print(f"{len(combos)} conversaciones × hasta {config.turns} turnos · "
              f"{config.concurrency} a la vez · CSV: {config.output}")

        sink = CsvSink(config.output)
        semaphore = asyncio.Semaphore(config.concurrency)
        done_count = 0
        started = time.perf_counter()

        async def one(scenario: Scenario, size: int, repeat: int) -> SessionSummary:
            nonlocal done_count
            async with semaphore:
                summary = await run_session(client, scenario, size, repeat, pdfs.get(size), config, sink, run_id)
            done_count += 1
            notes = f" · {'; '.join(summary.notes)}" if summary.notes else ""
            print(f"[{done_count}/{len(combos)}] {scenario.name} · {size} KB · rep. {repeat}: "
                  f"{summary.turns} turnos, {summary.errors} con error, "
                  f"{summary.cost_usd:.4f} $, {summary.seconds:.0f} s{notes}")
            return summary

        try:
            summaries = await asyncio.gather(*(one(*combo) for combo in combos))
        finally:
            sink.close()
            await client.aclose()

    total_cost = sum(s.cost_usd for s in summaries)
    total_errors = sum(s.errors for s in summaries)
    # Con el modelo falso el coste es de mentira: tokens inventados a precio de
    # Haiku. Lo decimos para que nadie crea que se ha gastado dinero.
    simulated = " (simulado: no se ha gastado nada)" if config.fake_llm else ""
    print(f"Listo: {sink.rows} filas, {total_errors} turnos con error, {total_cost:.4f} $ en total{simulated}, "
          f"{(time.perf_counter() - started) / 60:.1f} min. CSV: {config.output}")
    _write_excel_next_to(config.output)
    print(f"Siguiente: las tablas del informe con  uv run python -m evals.stress.report --csv {config.output}")
    return 0


def _write_excel_next_to(csv_path: Path) -> None:
    """Deja al lado del CSV su versión legible en Excel (ver to_excel.py).

    Es una ayuda para revisar los resultados: si falta openpyxl o algo sale
    mal, se avisa y la batería no se da por fallida (el CSV ya está escrito).
    """
    try:
        from evals.stress.to_excel import write_excel
    except ImportError:
        print("Excel no generado: falta openpyxl (uv add openpyxl).")
        return
    try:
        print(f"Excel: {write_excel(csv_path)}")
    except Exception as error:  # el CSV es lo importante; el Excel, una ayuda
        print(f"Excel no generado: {error!r}")


def _int_list(text: str) -> list[int]:
    return [int(part) for part in text.split(",") if part.strip()]


def parse_args(argv: list[str] | None = None) -> RunConfig:
    parser = argparse.ArgumentParser(
        prog="python -m evals.stress.run",
        description="Stress test del CAG: conversaciones sintéticas contra la API de sesiones.",
    )
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--http", metavar="URL", help="API ya arrancada, p. ej. http://127.0.0.1:8001")
    where.add_argument("--in-process", action="store_true", help="API en memoria con el modelo real (.env)")
    where.add_argument("--fake-llm", action="store_true", help="API en memoria con un modelo de mentira (gratis)")
    parser.add_argument("--scenarios", default="growing,pivot,contradiction")
    parser.add_argument("--attachment-sizes", default="0,5,20,50,100", help="KB de texto; 0 = sin adjunto")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--turns", type=int, default=TURNS_PER_SCENARIO, help="turnos por conversación (máx. 20)")
    parser.add_argument("--concurrency", type=int, default=2, help="conversaciones a la vez")
    parser.add_argument("--retries", type=int, default=2, help="reintentos de un turno con fallo pasajero")
    parser.add_argument("--budget-ms", type=int, default=DEFAULT_LATENCY_BUDGET_MS)
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_COST_BUDGET_USD)
    parser.add_argument("--model", help="modelo para --in-process / --fake-llm (con --http lo decide la API)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    return RunConfig(
        scenarios=args.scenarios.split(","),
        sizes_kb=_int_list(args.attachment_sizes),
        repeats=max(1, args.repeats),
        turns=max(1, min(args.turns, TURNS_PER_SCENARIO)),
        http=args.http,
        in_process=args.in_process,
        fake_llm=args.fake_llm,
        model=args.model,
        concurrency=max(1, args.concurrency),
        retries=max(0, args.retries),
        retry_wait_s=(0.0,) if args.fake_llm else (5.0, 15.0),
        budget_ms=args.budget_ms,
        budget_usd=args.budget_usd,
        output=args.output,
    )


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
