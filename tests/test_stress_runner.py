"""Tests del runner del stress test (sesión 6, ``evals/stress/run.py``).

Todos con el modelo de mentira (``evals/stress/fake_llm.py``) y la API en
memoria: sin clave, sin red y sin coste. Como el modelo falso es previsible,
los resultados de la deriva de memoria también lo son, y se pueden comprobar
turno a turno.

Se ejecutan con:

    uv run python -m pytest tests/test_stress_runner.py -v
"""

import csv

import httpx
import pytest

from app.services import conversation_service
from app.services.attachments.common import LLMProviderError
from app.services.turn_observer import REQUIRED_FIELDS
from evals.stress.fake_llm import FakeStressLLM
from evals.stress.run import (
    FIELDNAMES,
    RunConfig,
    _in_process_settings,
    get_snapshot,
    main_async,
    make_app,
    parse_args,
    post_turn,
)


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def details(row) -> dict[str, str]:
    """"budget_80k=1;team_5=0" -> {"budget_80k": "1", "team_5": "0"}."""
    text = row["memory_drift_details"] or row["attachment_recall_details"]
    return dict(item.split("=") for item in text.split(";") if item)


def fake_config(tmp_path, **overrides) -> RunConfig:
    values = dict(
        scenarios=["contradiction"], sizes_kb=[0], repeats=1, turns=14, fake_llm=True,
        concurrency=2, retry_wait_s=(0.0,), output=tmp_path / "results.csv", fixtures_dir=tmp_path,
    )
    values.update(overrides)
    return RunConfig(**values)


async def test_fake_run_writes_one_row_per_turn_with_all_the_columns(tmp_path):
    config = fake_config(tmp_path, sizes_kb=[0, 5])
    assert await main_async(config) == 0

    rows = read_csv(config.output)
    assert list(rows[0]) == FIELDNAMES
    assert all(field in FIELDNAMES for field in REQUIRED_FIELDS)  # los 13 del enunciado
    assert len(rows) == 2 * 14
    assert {row["outcome"] for row in rows} == {"done"}

    by_size = {size: [r for r in rows if r["attachment_kb"] == size] for size in ("0", "5")}
    for size_rows in by_size.values():
        size_rows.sort(key=lambda r: int(r["turn_index"]))
        costs = [float(r["cumulative_cost_usd"]) for r in size_rows]
        assert costs == sorted(costs), "el coste acumulado no puede bajar"
        assert max(int(r["messages_in_window"]) for r in size_rows) == 12  # ventana de 6 turnos

    first_with_pdf = by_size["5"][0]
    assert int(first_with_pdf["attachments_total_chars"]) == pytest.approx(5_000, rel=0.03)
    assert by_size["0"][0]["attachments_total_chars"] == "0"


async def test_contradiction_budget_lives_while_its_turn_is_in_the_window(tmp_path):
    """El 80.000 se dice en el turno 8 y no tiene sitio en la memoria.

    Con un modelo que no lo repite, solo vive en el historial: está tras el
    turno 13 (la ventana guarda los turnos 8 a 13) y desaparece en el 14.
    El equipo de 5, en cambio, está en la memoria y no se pierde.
    """
    config = fake_config(tmp_path)
    await main_async(config)
    rows = {int(r["turn_index"]): details(r) for r in read_csv(config.output)}

    assert rows[9]["budget_80k"] == "1"
    assert rows[9]["budget_30k_gone_reply"] == "1"
    assert rows[9]["team_3_gone_memory"] == "1"
    assert rows[13]["budget_80k"] == "1"
    assert rows[14]["budget_80k"] == "0"
    assert rows[14]["team_5"] == "1"


async def test_pivot_react_stays_in_memory_and_comes_back_in_the_replies(tmp_path):
    """Un modelo que respeta la memoria repite React, porque la memoria no lo borra."""
    config = fake_config(tmp_path, scenarios=["pivot"], turns=7)
    await main_async(config)
    rows = {int(r["turn_index"]): details(r) for r in read_csv(config.output)}

    assert rows[6]["react_gone_memory"] == "0"
    assert rows[6]["react_gone_reply"] == "0"
    assert rows[6]["flutter"] == "1"


async def test_attachment_recall_only_while_the_model_has_the_document(tmp_path):
    config = fake_config(tmp_path, sizes_kb=[5], turns=2)
    await main_async(config)
    rows = sorted(read_csv(config.output), key=lambda r: int(r["turn_index"]))

    assert rows[0]["attachment_recall_score"] == "1.0"
    assert details(rows[0]) == {"pdf_sap": "1", "pdf_migration": "1", "pdf_iso": "1"}
    assert rows[1]["attachment_recall_score"] == "0.0"


async def test_400kb_attachment_fails_loudly_and_the_conversation_stops(tmp_path):
    config = fake_config(tmp_path, sizes_kb=[400], turns=3)
    await main_async(config)
    rows = read_csv(config.output)

    assert len(rows) == 1
    assert rows[0]["outcome"] == "error"
    assert rows[0]["http_status"] == "200"  # el error llega dentro del stream (SSE)
    assert "máximo" in rows[0]["error_detail"]


async def test_post_turn_retries_a_passing_provider_failure_and_matches_the_snapshot(tmp_path, monkeypatch):
    """Un fallo pasajero del proveedor se reintenta; y lo que llega en el done es
    lo mismo que guarda la sesión (GET /sessions/{id} -> last_turn_observed)."""
    fake = FakeStressLLM()
    calls = {"n": 0}

    async def flaky(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMProviderError("Se ha alcanzado el límite de peticiones del proveedor.")
        async for chunk in fake.stream_reply(messages, **kwargs):
            yield chunk

    monkeypatch.setattr(conversation_service.path_b_local, "stream_reply", flaky)
    config = fake_config(tmp_path)
    app = make_app(_in_process_settings(config))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        session_id = (await client.post("/sessions")).json()["session_id"]
        result = await post_turn(client, session_id, "El proyecto se llama Nimbus.", None, config)
        snapshot = await get_snapshot(client, session_id)

    assert (result.final_event, result.attempts) == ("done", 2)
    assert result.final_data["turn_observed"] == snapshot["last_turn_observed"]


def test_command_line_defaults_follow_the_exercise():
    config = parse_args(["--http", "http://127.0.0.1:8001"])
    assert config.scenarios == ["growing", "pivot", "contradiction"]
    assert config.sizes_kb == [0, 5, 20, 50, 100]
    assert (config.repeats, config.turns) == (3, 20)
    with pytest.raises(SystemExit):
        parse_args(["--repeats", "1"])  # hay que elegir --http, --in-process o --fake-llm


async def test_resume_keeps_finished_conversations_and_redoes_the_unfinished_one(tmp_path):
    """Si la batería se corta, --resume conserva lo completo y repite lo que quedó a medias."""
    config = fake_config(tmp_path, sizes_kb=[0, 5], turns=4, concurrency=1)
    await main_async(config)
    rows = read_csv(config.output)
    first_run = rows[0]["run_id"]

    # Simulamos el corte: la conversación sin PDF entera y solo 2 turnos de la otra.
    finished = [r for r in rows if r["attachment_kb"] == "0"]
    unfinished = [r for r in rows if r["attachment_kb"] == "5"][:2]
    with config.output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(finished + unfinished)

    import asyncio
    await asyncio.sleep(1.1)  # para que el identificador de la nueva ejecución sea otro
    config.resume = True
    await main_async(config)
    rows = read_csv(config.output)

    assert len(rows) == 8
    assert {r["run_id"] for r in rows if r["attachment_kb"] == "0"} == {first_run}  # conservada
    redone = [r for r in rows if r["attachment_kb"] == "5"]
    assert sorted(int(r["turn_index"]) for r in redone) == [1, 2, 3, 4]  # repetida entera
    assert {r["run_id"] for r in redone} != {first_run}
