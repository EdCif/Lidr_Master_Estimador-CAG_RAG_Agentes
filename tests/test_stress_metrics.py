"""Tests de las métricas del stress test (sesión 6, ``evals/stress/metrics.py``).

El enunciado pide, como mínimo, para cada métrica: una que pasa, una que
falla y una con datos límite. Aquí están las tres de cada una, y algún caso
más de la deriva de memoria, que es la más delicada.

Se ejecutan con:

    uv run python -m pytest tests/test_stress_metrics.py -v
"""

import pytest

from evals.stress.metrics import (
    CostBudgetMetric,
    LatencyBudgetMetric,
    MemoryDriftMetric,
    MetricResult,
    run_all_metrics,
    snapshot_texts,
)


def make_snapshot(metadata=None, history=None) -> dict:
    """Una foto de sesión como la que devuelve GET /sessions/{id}, solo con lo que se usa."""
    return {
        "project_metadata": metadata
        or {"project_name": None, "assumed_team_size": None, "mentioned_technologies": [], "agreed_scope": None},
        "history": history or [],
    }


NIMBUS_MEMORY = {
    "project_name": "Nimbus",
    "assumed_team_size": 3,
    "mentioned_technologies": ["React", "FastAPI", "Flutter"],
    "agreed_scope": None,
}


# ---------------------------------------------------------------------------
# Presupuesto de latencia
# ---------------------------------------------------------------------------

def test_latency_within_budget_passes():
    result = LatencyBudgetMetric(budget_ms=20_000).evaluate({"latency_ms": 15_400})
    assert result == MetricResult(
        name="latency_budget",
        score=1.0,
        passed=True,
        details={"latency_ms": 15_400, "budget_ms": 20_000, "margin_ms": 4_600},
    )


def test_latency_over_budget_fails():
    result = LatencyBudgetMetric(budget_ms=20_000).evaluate({"latency_ms": 23_000})
    assert (result.score, result.passed) == (0.0, False)
    assert result.details["margin_ms"] == -3_000


def test_latency_exactly_at_the_budget_passes_and_missing_latency_fails():
    metric = LatencyBudgetMetric(budget_ms=20_000)
    assert metric.evaluate({"latency_ms": 20_000}).passed
    missing = metric.evaluate({})
    assert not missing.passed
    assert missing.details["reason"] == "falta latency_ms"
    with pytest.raises(ValueError):
        LatencyBudgetMetric(budget_ms=0)


# ---------------------------------------------------------------------------
# Presupuesto de coste
# ---------------------------------------------------------------------------

def test_cost_within_budget_passes():
    result = CostBudgetMetric(budget_usd=0.05).evaluate({"cost_usd": 0.0011})
    assert (result.name, result.score, result.passed) == ("cost_budget", 1.0, True)
    assert result.details["margin_usd"] == pytest.approx(0.0489)


def test_cost_over_budget_fails():
    result = CostBudgetMetric(budget_usd=0.05).evaluate({"cost_usd": 0.12})
    assert (result.score, result.passed) == (0.0, False)


def test_cost_exactly_at_the_budget_passes_and_unknown_cost_fails():
    """Un coste que no se conoce no cumple el presupuesto: no se puede comprobar."""
    metric = CostBudgetMetric(budget_usd=0.05)
    assert metric.evaluate({"cost_usd": 0.05}).passed
    unknown = metric.evaluate({"cost_usd": None})
    assert not unknown.passed
    assert unknown.details["reason"] == "falta cost_usd"


# ---------------------------------------------------------------------------
# Deriva de memoria
# ---------------------------------------------------------------------------

def test_fact_kept_in_the_memory_passes():
    """El nombre del proyecto, dicho en el turno 1, sigue en la ficha."""
    result = MemoryDriftMetric("nimbus").evaluate(make_snapshot(NIMBUS_MEMORY))
    assert (result.name, result.score, result.passed) == ("memory_drift", 1.0, True)
    assert result.details["found_in"] == ["metadata"]


def test_fact_that_left_the_window_and_is_not_in_memory_fails():
    """El presupuesto no tiene sitio en la ficha: cuando su turno sale de la
    ventana, ya no queda en ninguna parte. Es la deriva que queremos ver."""
    snapshot = make_snapshot(NIMBUS_MEMORY, history=[
        {"role": "user", "content": "Añadid exportar a CSV."},
        {"role": "assistant", "content": "Estimación: 420 horas."},
    ])
    result = MemoryDriftMetric("30.000 euros").evaluate(snapshot)
    assert (result.score, result.passed) == (0.0, False)
    assert result.details["found_in"] == []


def test_fact_still_in_the_window_is_found_in_history_but_not_in_memory():
    snapshot = make_snapshot(NIMBUS_MEMORY, history=[
        {"role": "user", "content": "El presupuesto aprobado es de 30.000 euros."},
        {"role": "assistant", "content": "Estimación: 300 horas."},
    ])
    assert MemoryDriftMetric("30.000 euros").evaluate(snapshot).details["found_in"] == ["history"]
    # Si solo miramos la memoria, no está: así se ve qué sitio lo sostiene.
    assert not MemoryDriftMetric("30.000 euros", where=("metadata",)).evaluate(snapshot).passed


def test_match_ignores_case_but_requires_the_whole_word():
    snapshot = make_snapshot(history=[
        {"role": "user", "content": "La integración con SAP es obligatoria."},
        {"role": "assistant", "content": "El módulo antiguo desaparece."},
    ])
    assert MemoryDriftMetric("sap").evaluate(snapshot).details["found_in"] == ["history"]
    # "sap" dentro de "desaparece" no cuenta: la respuesta no lo menciona.
    assert not MemoryDriftMetric("sap", where=("reply",)).evaluate(snapshot).passed
    assert MemoryDriftMetric("sap", where=("reply",), whole_word=False).evaluate(snapshot).passed


def test_expected_false_checks_that_a_discarded_fact_is_gone():
    """Tras "descartad React", la ficha sigue sumando React: la métrica lo delata."""
    still_there = MemoryDriftMetric("react", where=("metadata",), expected=False)
    assert not still_there.evaluate(make_snapshot(NIMBUS_MEMORY)).passed

    clean_memory = {**NIMBUS_MEMORY, "mentioned_technologies": ["FastAPI", "Flutter"]}
    assert still_there.evaluate(make_snapshot(clean_memory)).passed


def test_empty_session_and_path_a_blocks_do_not_break_the_search():
    # Sesión recién creada: nada que encontrar, pero sin errores.
    assert not MemoryDriftMetric("nimbus").evaluate(make_snapshot()).passed
    # Camino A: el mensaje del cliente es una lista de bloques (documento + texto).
    snapshot = make_snapshot(history=[
        {"role": "user", "content": [
            {"type": "document", "source": {"type": "file", "file_id": "file_abc"}, "title": "spec.pdf"},
            {"type": "text", "text": "El proyecto se llama Nimbus."},
        ]},
        {"role": "assistant", "content": "Estimación para Nimbus: 300 horas."},
    ])
    assert MemoryDriftMetric("nimbus").evaluate(snapshot).details["found_in"] == ["history", "reply"]


def test_snapshot_texts_uses_the_prompt_lines_and_the_last_reply():
    snapshot = make_snapshot(NIMBUS_MEMORY, history=[
        {"role": "user", "content": "Primera"},
        {"role": "assistant", "content": "Respuesta 1"},
        {"role": "user", "content": "Segunda"},
        {"role": "assistant", "content": "Respuesta 2"},
    ])
    texts = snapshot_texts(snapshot)
    assert texts["metadata"].splitlines()[0] == "Proyecto: Nimbus"
    assert "Equipo asumido: 3 personas" in texts["metadata"]
    assert texts["reply"] == "Respuesta 2"
    assert "Primera" in texts["history"] and "Respuesta 2" in texts["history"]


def test_invalid_drift_metric_is_rejected():
    with pytest.raises(ValueError):
        MemoryDriftMetric("   ")
    with pytest.raises(ValueError):
        MemoryDriftMetric("nimbus", where=("summary",))


# ---------------------------------------------------------------------------
# Varias métricas a la vez
# ---------------------------------------------------------------------------

def test_run_all_metrics_gives_each_metric_what_it_reads():
    results = run_all_metrics(
        [LatencyBudgetMetric(20_000), CostBudgetMetric(0.05), MemoryDriftMetric("nimbus")],
        observation={"latency_ms": 15_000, "cost_usd": 0.2},
        snapshot=make_snapshot(NIMBUS_MEMORY),
    )
    assert [(r.name, r.passed) for r in results] == [
        ("latency_budget", True),
        ("cost_budget", False),
        ("memory_drift", True),
    ]
