"""Tests de integración de la sesión 5, con pytest y httpx.AsyncClient (Paso 03).

Son los tres que pide el enunciado. Cada uno recorre la API como lo haría un
cliente de verdad (crear sesión, mandar el formulario, leer el streaming) y
solo el modelo es falso (ver ``FakeLLM`` en conftest.py).

Se ejecutan con:

    uv run python -m pytest tests/test_integration_sessions.py -v

Los fixtures ``client``, ``fake_llm`` y ``store`` llegan solos desde
conftest.py: basta con nombrarlos como parámetros del test.
"""

import re

from api_helpers import parse_sse
from doc_factories import make_text_pdf


async def new_session(client) -> str:
    """Crea una sesión con POST /sessions y devuelve su id."""
    response = await client.post("/sessions")
    assert response.status_code == 201
    return response.json()["session_id"]


async def estimate(client, session_id: str, transcript: str, files=None) -> dict:
    """Manda un turno a /estimate y devuelve los datos del evento final "done"."""
    response = await client.post(
        f"/sessions/{session_id}/estimate",
        data={"transcript": transcript, "attachment_mode": "local"},
        files=files,
    )
    assert response.status_code == 200
    events = parse_sse(response.text)
    name, data = events[-1]
    assert name == "done", f"el turno no terminó bien: {events[-1]}"
    return data


def metadata_block(system_prompt: str) -> str:
    """El contenido del bloque <project_metadata> de un system prompt."""
    match = re.search(r"<project_metadata>\n(.*?)</project_metadata>", system_prompt, re.S)
    assert match, "el system prompt no tiene bloque <project_metadata>"
    return match.group(1)


# ---------------------------------------------------------------------------
# 1. Dos peticiones en la misma sesión: la memoria se actualiza
# ---------------------------------------------------------------------------

async def test_two_requests_in_the_same_session_update_the_project_metadata(client, fake_llm):
    """El cliente presenta el proyecto y, en un segundo turno, cambia el equipo.

    Primer turno: la ficha (project_metadata) estaba vacía y se rellena con lo
    que dice el cliente (nombre, equipo, tecnologías). Segundo turno: el equipo
    cambia de 3 a 4 personas y se acuerda el alcance; lo demás se conserva
    aunque no se repita. Además, la memoria del primer turno viaja dentro del
    system prompt de la SEGUNDA llamada al modelo.
    """
    session_id = await new_session(client)

    first = await estimate(
        client,
        session_id,
        "El proyecto se llama Portal de Reservas. Las tecnologías son React y FastAPI. "
        "Suponed un equipo de 3 personas.",
    )
    memory = first["project_metadata"]
    assert memory["project_name"] == "Portal de Reservas"
    assert memory["assumed_team_size"] == 3
    assert memory["mentioned_technologies"] == ["React", "FastAPI"]
    assert memory["agreed_scope"] is None

    second = await estimate(
        client,
        session_id,
        "Cambio de planes: un equipo de 4 personas, y hemos acordado que la primera fase no incluye la app móvil.",
    )
    memory = second["project_metadata"]
    assert memory["assumed_team_size"] == 4  # el dato nuevo sustituye al anterior
    assert memory["agreed_scope"] == "la primera fase no incluye la app móvil"
    assert memory["project_name"] == "Portal de Reservas"  # no se repitió, pero no se pierde
    assert memory["mentioned_technologies"] == ["React", "FastAPI"]

    # El system prompt de la 1.ª llamada llevaba la memoria vacía; el de la
    # 2.ª, los hechos que se extrajeron del primer turno.
    first_system, second_system = fake_llm.calls[0][0], fake_llm.calls[1][0]
    assert first_system["role"] == second_system["role"] == "system"
    assert metadata_block(first_system["content"]) == ""
    assert "Proyecto: Portal de Reservas" in metadata_block(second_system["content"])
    assert "Equipo asumido: 3 personas" in metadata_block(second_system["content"])

    # Y GET /sessions/{id} enseña la memoria y el historial por separado.
    state = (await client.get(f"/sessions/{session_id}")).json()
    assert state["project_metadata"]["assumed_team_size"] == 4
    assert len(state["history"]) == 4  # 2 turnos = 2 preguntas + 2 respuestas


# ---------------------------------------------------------------------------
# 2. Un PDF adjunto influye en la estimación
# ---------------------------------------------------------------------------

async def test_an_attached_pdf_changes_the_estimation(client, fake_llm):
    """La misma transcripción, sin y con un PDF que dice que el backend irá en Kubernetes.

    Es un test cualitativo: el modelo falso añade horas si ve "Kubernetes" en
    lo que le llega. Ese dato solo está en el PDF, así que si la estimación
    cambia es que el texto del documento (extraído de verdad con pypdf, en el
    proceso aparte del Camino B) ha llegado al modelo. También la memoria lo
    refleja: Kubernetes aparece entre las tecnologías solo con el adjunto.
    """
    transcript = "Necesitamos un portal web para reservar salas, con React."
    pdf = make_text_pdf("Arquitectura: el backend se desplegara en Kubernetes.")

    without_pdf = await estimate(client, await new_session(client), transcript)
    with_pdf = await estimate(
        client,
        await new_session(client),
        transcript,
        files=[("attachments", ("especificacion.pdf", pdf, "application/pdf"))],
    )

    assert without_pdf["estimation"] != with_pdf["estimation"]
    assert "300 horas" in without_pdf["estimation"]
    assert "420 horas" in with_pdf["estimation"]
    assert "Kubernetes" not in without_pdf["project_metadata"]["mentioned_technologies"]
    assert "Kubernetes" in with_pdf["project_metadata"]["mentioned_technologies"]

    # Lo que recibió el modelo: la transcripción y, tras el separador, el PDF.
    question_with_pdf = fake_llm.calls[1][-1]["content"]
    assert "--- attachment: especificacion.pdf ---" in question_with_pdf
    assert "Kubernetes" in question_with_pdf


# ---------------------------------------------------------------------------
# 3. Ocho turnos: el historial enviado nunca pasa de MAX_TURNS
# ---------------------------------------------------------------------------

async def test_eight_turns_never_send_more_than_max_turns(client, fake_llm, store):
    """Ocho turnos seguidos en una sesión con ventana de 6 (MAX_TURNS).

    En CADA llamada al modelo comprobamos que el system prompt va el primero
    y solo una vez, y que no viajan más de 6 turnos (contando la pregunta
    nueva). Al final, la ventana tiene 6 turnos y se han descartado 2.

    Y lo más importante: el nombre del proyecto se dijo en el turno 1, que ya
    salió de la ventana, pero sigue en la memoria y en el system prompt del
    turno 8. Es la diferencia entre historial y memoria.
    """
    assert store.max_turns == 6
    session_id = await new_session(client)

    await estimate(client, session_id, "Turno 1: el proyecto se llama Portal de Reservas.")
    for number in range(2, 9):
        await estimate(client, session_id, f"Turno {number}: otro ajuste de la estimación.")

    assert len(fake_llm.calls) == 8
    for messages in fake_llm.calls:
        roles = [message["role"] for message in messages]
        assert roles[0] == "system" and roles.count("system") == 1
        assert roles.count("user") <= store.max_turns

    # En la 8.ª llamada viajan los turnos 3 a 7 y la pregunta nueva (la 8).
    last_call_questions = [m["content"] for m in fake_llm.calls[-1] if m["role"] == "user"]
    assert [q.split(":")[0] for q in last_call_questions] == [f"Turno {n}" for n in range(3, 9)]

    state = (await client.get(f"/sessions/{session_id}")).json()
    assert state["turn_count"] == 6
    assert state["discarded_turns"] == 2

    # El turno 1 ya no viaja, pero su hecho sigue en la memoria y en el prompt.
    assert state["project_metadata"]["project_name"] == "Portal de Reservas"
    assert "Proyecto: Portal de Reservas" in metadata_block(fake_llm.calls[-1][0]["content"])
