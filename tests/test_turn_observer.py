"""Tests de la observación del turno (sesión 06, ``app/services/turn_observer.py``).

Prueban las piezas sueltas, sin pasar por la API: el cronómetro, cómo se
escribe la memoria como texto y la observación que se guarda y se registra.
Los tests del endpoint (que ``turn_observed`` llega en el evento ``done``)
están en ``tests/test_sessions_api.py``.

Se ejecutan con:

    uv run python -m pytest tests/test_turn_observer.py -v
"""

import json
import logging

import pytest

from app.services.attachments.common import ReplyStats
from app.services.sessions import ProjectMetadata, Session
from app.services.turn_observer import (
    REQUIRED_FIELDS,
    TURN_OBSERVED,
    TurnTimer,
    count_anchors,
    observe_turn,
    render_metadata_facts,
)


class FakeClock:
    """Reloj de mentira: cada vez que se consulta, avanza los segundos indicados."""

    def __init__(self, *readings: float) -> None:
        self.readings = list(readings)

    def __call__(self) -> float:
        return self.readings.pop(0)


def make_session(metadata: ProjectMetadata | None = None) -> Session:
    return Session("sesion-de-prueba", system_prompt_builder=lambda m: "SYSTEM", metadata=metadata)


# ---------------------------------------------------------------------------
# El cronómetro
# ---------------------------------------------------------------------------

def test_timer_measures_turn_first_token_and_llm_call():
    # Lecturas del reloj: empieza el turno (0 s), sale la petición (1 s),
    # primer trozo (1,25 s), fin del modelo (3 s) y final del turno (3,5 s).
    # El segundo trozo ni siquiera mira el reloj: ya tenemos el primero.
    timer = TurnTimer(clock=FakeClock(0.0, 1.0, 1.25, 3.0, 3.5))
    timer.mark_llm_start()
    timer.mark_first_delta()
    timer.mark_first_delta()  # el segundo trozo se ignora
    timer.mark_llm_end()

    assert timer.ttft_ms() == 250
    assert timer.llm_ms() == 2000
    assert timer.elapsed_ms() == 3500


def test_timer_without_llm_call_has_no_ttft_nor_llm_time():
    """Si el turno falla antes de llamar al modelo (un PDF sin texto, por ejemplo)."""
    timer = TurnTimer(clock=FakeClock(0.0, 0.2))
    assert timer.ttft_ms() is None
    assert timer.llm_ms() is None
    assert timer.elapsed_ms() == 200


# ---------------------------------------------------------------------------
# La memoria como texto
# ---------------------------------------------------------------------------

def test_render_metadata_facts_uses_the_same_lines_as_the_prompt():
    metadata = ProjectMetadata(
        project_name="Nimbus",
        assumed_team_size=3,
        mentioned_technologies=["React", "FastAPI"],
        agreed_scope="la primera fase no incluye pagos",
    )
    expected = (
        "Proyecto: Nimbus\n"
        "Equipo asumido: 3 personas\n"
        "Tecnologías: React, FastAPI\n"
        "Alcance acordado: la primera fase no incluye pagos"
    )
    assert render_metadata_facts(metadata) == expected
    # También con el diccionario que devuelve GET /sessions/{id}.
    assert render_metadata_facts(metadata.model_dump()) == expected
    assert count_anchors(metadata) == 5  # nombre + equipo + 2 tecnologías + alcance


def test_empty_memory_is_empty_text_and_zero_anchors():
    assert render_metadata_facts(ProjectMetadata()) == ""
    assert render_metadata_facts(None) == ""
    assert count_anchors(ProjectMetadata()) == 0


# ---------------------------------------------------------------------------
# La observación completa
# ---------------------------------------------------------------------------

def test_observe_turn_fills_the_13_fields_saves_them_and_logs_one_json_line(caplog):
    session = make_session(ProjectMetadata(project_name="Nimbus", mentioned_technologies=["React"]))
    session.history.add_turn("Pregunta", "Respuesta")
    stats = ReplyStats(model="claude-haiku-5-5", input_tokens=50_000, output_tokens=2_000, final_text="Respuesta")
    timer = TurnTimer(clock=FakeClock(0.0, 1.0, 1.5, 4.0, 4.2))
    timer.mark_llm_start()
    timer.mark_first_delta()
    timer.mark_llm_end()

    with caplog.at_level(logging.INFO, logger=TURN_OBSERVED):
        observation = observe_turn(
            session,
            turn_index=1,
            transcript="Pregunta",
            attachment_chars=5_000,
            attachments_count=1,
            stats=stats,
            attachment_mode="local",
            configured_model="claude-haiku-5-5",
            timer=timer,
            messages_sent=2,
            outcome="done",
        )

    data = observation.model_dump()
    assert [f for f in REQUIRED_FIELDS if f not in data] == []
    assert data["enriched_transcript_chars"] == len("Pregunta") + 5_000
    assert data["attachments_total_chars"] == 5_000
    assert data["messages_in_window"] == 2
    assert data["anchors_count"] == 2
    assert data["cost_usd"] == pytest.approx(0.006)  # ver test_model_costs.py
    assert data["latency_ms"] == 4200
    assert data["ttft_ms"] == 500
    assert data["llm_ms"] == 3000
    assert data["last_resolved_tier"] == "fixed:claude-haiku-5-5"
    assert data["tokens_source"] == "usage"
    assert data["metadata_chars"] == len("Proyecto: Nimbus\nTecnologías: React")
    assert session.last_turn_observed is observation

    # Una sola línea, en JSON, con el nombre del evento y los mismos datos.
    lines = [record.getMessage() for record in caplog.records if record.name == TURN_OBSERVED]
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert logged["event"] == "turn_observed"
    assert logged["turn_index"] == 1
    assert logged["cost_usd"] == pytest.approx(0.006)


def test_failed_turn_without_tokens_has_no_cost_and_says_why():
    session = make_session()
    stats = ReplyStats(model="claude-haiku-5-5")  # el proveedor no llegó a contestar

    observation = observe_turn(
        session,
        turn_index=1,
        transcript="Pregunta",
        attachment_chars=0,
        attachments_count=0,
        stats=stats,
        attachment_mode="local",
        configured_model="claude-haiku-5-5",
        timer=TurnTimer(),
        messages_sent=None,
        outcome="error",
        error_detail="No se ha podido conectar con el proveedor del LLM. Reintenta.",
    )

    assert observation.outcome == "error"
    assert observation.cost_usd is None
    assert observation.tokens_source == "missing"
    assert observation.messages_in_window == 0
