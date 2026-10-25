"""Tests de las conversaciones sintéticas (sesión 6, ``evals/stress/scenarios.py``).

No llaman a ningún modelo: comprueban que los guiones están bien hechos
(20 turnos, hechos con nombre único, sin repetirse) y que el extractor de
memoria de la sesión 5 reconoce de verdad las frases que plantan hechos en
la ficha. Si alguien retoca una frase y el extractor deja de entenderla, el
test lo dice antes de gastar dinero en la batería.

El último test simula la memoria turno a turno, sin modelo, y deja escrita
la previsión de lo que pasará con la ficha.

Se ejecutan con:

    uv run python -m pytest tests/test_stress_scenarios.py -v
"""

import re

import pytest

from app.services.metadata_extractor import facts_from_transcript, update_metadata_from_turn
from app.services.sessions import ProjectMetadata
from app.services.turn_observer import render_metadata_facts
from evals.stress.scenarios import (
    CHECKPOINTS,
    SCENARIOS,
    TURNS_PER_SCENARIO,
    get_scenarios,
)


def contains(text: str, fact_text: str) -> bool:
    """La misma búsqueda que MemoryDriftMetric: palabra completa, sin mayúsculas."""
    return re.search(rf"(?<!\w){re.escape(fact_text)}(?!\w)", text, re.IGNORECASE) is not None


def all_facts(scenario):
    return [(turn.turn_index, fact) for turn in scenario.turns for fact in turn.facts_to_remember]


@pytest.mark.parametrize("name", ["growing", "pivot", "contradiction"])
def test_each_scenario_has_20_numbered_turns_and_unique_fact_labels(name):
    scenario = SCENARIOS[name]
    assert [turn.turn_index for turn in scenario.turns] == list(range(1, TURNS_PER_SCENARIO + 1))
    assert all(turn.transcript.strip() for turn in scenario.turns)
    labels = [fact.label for _, fact in all_facts(scenario)]
    assert len(labels) == len(set(labels)), "cada hecho necesita un nombre único (sale en el CSV)"
    # Cada hecho se puede convertir en su métrica (no está vacío, sitios válidos).
    for _, fact in all_facts(scenario):
        fact.metric()
    assert max(CHECKPOINTS) <= TURNS_PER_SCENARIO


@pytest.mark.parametrize("name", ["growing", "pivot", "contradiction"])
def test_memory_facts_are_recognised_by_the_session_5_extractor(name):
    """Cada hecho que debe acabar en la ficha lo saca el extractor de la frase de su turno."""
    for turn in SCENARIOS[name].turns:
        memory_text = render_metadata_facts(facts_from_transcript(turn.transcript))
        for fact in turn.facts_to_remember:
            if fact.where == ("metadata",) and fact.expected:
                assert contains(memory_text, fact.text), (
                    f"{name}, turno {turn.turn_index}: el extractor no saca «{fact.text}» "
                    f"de la frase. Ficha resultante:\n{memory_text}"
                )


@pytest.mark.parametrize("name", ["growing", "pivot", "contradiction"])
def test_facts_are_not_repeated_by_the_client_while_they_are_checked(name):
    """Si el cliente repitiera un hecho, se "recordaría" por habérselo dicho otra vez."""
    scenario = SCENARIOS[name]
    for planted_at, fact in all_facts(scenario):
        last = fact.valid_until or TURNS_PER_SCENARIO
        for turn in scenario.turns:
            if planted_at < turn.turn_index <= last:
                assert not contains(turn.transcript, fact.text), (
                    f"{name}: «{fact.text}» (turno {planted_at}) se repite en el turno {turn.turn_index}"
                )


def test_facts_to_check_follow_the_contradiction():
    contradiction = SCENARIOS["contradiction"]
    before = {fact.label for _, fact in contradiction.facts_to_check(at_turn=5)}
    assert before == {"project_name", "team_3", "sso", "budget_30k", "weekly_calendar"}

    after = {fact.label for _, fact in contradiction.facts_to_check(at_turn=9)}
    assert {"budget_80k", "team_5", "budget_30k_gone_reply", "team_3_gone_memory"} <= after
    assert not {"team_3", "budget_30k"} & after  # ya no se comprueba que se recuerden
    # Solo hechos de turnos anteriores: lo del turno 9 se comprueba desde el 10.
    assert all(planted_at < 9 for planted_at, _ in contradiction.facts_to_check(at_turn=9))


def test_get_scenarios_by_name():
    assert [s.name for s in get_scenarios(["pivot", " growing "])] == ["pivot", "growing"]
    with pytest.raises(ValueError):
        get_scenarios(["growing", "otro"])


def test_memory_prediction_only_react_survives_the_pivot():
    """Previsión de la ficha, simulada sin modelo, solo con lo que dice el cliente.

    Se aplica el extractor turno a turno (como hace la API tras cada respuesta,
    pero sin el bloque <hechos_del_proyecto> del modelo) y, tras cada turno N,
    se evalúan los hechos de la memoria plantados en turnos anteriores (k < N),
    igual que hará el runner.

    Previsión: todos se conservan salvo uno. Tras "descartad React", la ficha
    sigue teniendo React, porque merge_metadata SUMA tecnologías y nunca las
    borra (límite conocido de la sesión 5, documentado en metadata_extractor.py).
    El equipo de 3 sí se sustituye por el de 5: los datos de un solo valor
    se reemplazan. Con el modelo de verdad, su bloque de hechos puede además
    reescribir la ficha: eso es lo que medirá la batería.
    """
    failures = set()
    for scenario in SCENARIOS.values():
        memory = ProjectMetadata()
        for turn in scenario.turns:
            memory = update_metadata_from_turn(memory, turn.transcript, reply="")
            snapshot = {"project_metadata": memory.model_dump(), "history": []}
            for _, fact in scenario.facts_to_check(at_turn=turn.turn_index):
                if fact.where == ("metadata",) and not fact.metric().evaluate(snapshot).passed:
                    failures.add((scenario.name, fact.label, turn.turn_index))

    # React se descarta en el turno 5 y se comprueba desde el 6 hasta el 20.
    assert failures == {("pivot", "react_gone_memory", n) for n in range(6, 21)}
