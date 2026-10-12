"""Pruebas de las sesiones (sesión 05): memoria, historial con ventana y almacén.

No llaman a ningún LLM ni necesitan claves: el system prompt lo genera una
función de prueba que solo escribe la ficha del proyecto en texto.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from app.services.sessions import (
    DEFAULT_MAX_TURNS,
    MAX_FACT_CHARS,
    MAX_TECHNOLOGIES,
    ConversationHistory,
    ProjectMetadata,
    Session,
    SessionExpiredError,
    SessionNotFoundError,
    SessionStore,
)


class FakeClock:
    """Reloj de mentira: marca la hora que digamos y se puede adelantar.

    Así probamos que una sesión caduca a la hora sin esperar una hora.
    """

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def fake_prompt_builder(metadata: ProjectMetadata) -> str:
    """System prompt de mentira: deja ver qué ficha había al generarlo."""
    return f"SYSTEM | proyecto={metadata.project_name}"


def user_messages(messages: list[dict]) -> list[str]:
    return [m["content"] for m in messages if m["role"] == "user"]


class ProjectMetadataTests(unittest.TestCase):
    def test_new_metadata_is_empty(self):
        metadata = ProjectMetadata()

        self.assertTrue(metadata.is_empty())
        self.assertIsNone(metadata.project_name)
        self.assertEqual(metadata.mentioned_technologies, [])

    def test_any_known_fact_makes_it_non_empty(self):
        self.assertFalse(ProjectMetadata(project_name="Portal de reservas").is_empty())
        self.assertFalse(ProjectMetadata(assumed_team_size=3).is_empty())
        self.assertFalse(ProjectMetadata(mentioned_technologies=["FastAPI"]).is_empty())
        self.assertFalse(ProjectMetadata(agreed_scope="Sin pagos").is_empty())

    def test_blank_texts_count_as_unknown(self):
        metadata = ProjectMetadata(project_name="   ", agreed_scope="")

        self.assertIsNone(metadata.project_name)
        self.assertIsNone(metadata.agreed_scope)
        self.assertTrue(metadata.is_empty())

    def test_technologies_are_cleaned_and_deduplicated_keeping_first_spelling(self):
        metadata = ProjectMetadata(
            mentioned_technologies=[" React ", "react", "", "PostgreSQL", "REACT", "FastAPI"]
        )

        self.assertEqual(metadata.mentioned_technologies, ["React", "PostgreSQL", "FastAPI"])

    def test_technologies_given_as_comma_separated_text_are_accepted(self):
        metadata = ProjectMetadata(mentioned_technologies="Python, Streamlit ,python")

        self.assertEqual(metadata.mentioned_technologies, ["Python", "Streamlit"])

    def test_team_size_is_tolerant_with_what_an_extractor_may_return(self):
        cases = {
            3: 3,
            "4": 4,
            5.0: 5,
            "2.0": 2,
            0: None,
            -2: None,
            "unas cuantas": None,
            True: None,
            None: None,
            float("inf"): None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ProjectMetadata(assumed_team_size=raw).assumed_team_size, expected)

    def test_unknown_fields_from_the_extractor_are_ignored(self):
        metadata = ProjectMetadata.model_validate(
            {"project_name": "CRM", "presupuesto": "50.000 €"}
        )

        self.assertEqual(metadata.project_name, "CRM")
        self.assertNotIn("presupuesto", metadata.model_dump())

    def test_assignment_is_validated_too(self):
        metadata = ProjectMetadata()
        metadata.mentioned_technologies = ["Vue", "vue"]

        self.assertEqual(metadata.mentioned_technologies, ["Vue"])
        with self.assertRaises(ValidationError):
            metadata.project_name = ["no", "es", "un", "texto"]


class ConversationHistoryTests(unittest.TestCase):
    def make_history(self, max_turns=DEFAULT_MAX_TURNS, prompt="SYSTEM"):
        return ConversationHistory(system_prompt_factory=lambda: prompt, max_turns=max_turns)

    def test_default_window_is_six_turns(self):
        self.assertEqual(DEFAULT_MAX_TURNS, 6)
        self.assertEqual(self.make_history().max_turns, 6)

    def test_window_must_allow_at_least_one_turn(self):
        with self.assertRaises(ValueError):
            self.make_history(max_turns=0)

    def test_empty_history_only_sends_the_system_prompt(self):
        history = self.make_history(prompt="Eres un estimador")

        self.assertEqual(
            history.to_messages_list(),
            [{"role": "system", "content": "Eres un estimador"}],
        )

    def test_turns_are_stored_as_user_assistant_pairs(self):
        history = self.make_history()
        history.add_turn("pregunta 1", "respuesta 1")

        self.assertEqual(history.turn_count, 1)
        self.assertEqual(
            history.to_messages_list(),
            [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "pregunta 1"},
                {"role": "assistant", "content": "respuesta 1"},
            ],
        )

    def test_oldest_pairs_are_discarded_when_window_is_exceeded(self):
        history = self.make_history(max_turns=3)
        for number in range(1, 6):
            history.add_turn(f"pregunta {number}", f"respuesta {number}")

        messages = history.to_messages_list()

        self.assertEqual(history.turn_count, 3)
        self.assertEqual(history.discarded_turns, 2)
        self.assertEqual(user_messages(messages), ["pregunta 3", "pregunta 4", "pregunta 5"])
        # El system sigue el primero y nunca se parte un turno por la mitad.
        self.assertEqual(
            [m["role"] for m in messages],
            ["system", "user", "assistant", "user", "assistant", "user", "assistant"],
        )

    def test_pending_question_counts_as_one_of_the_max_turns(self):
        history = self.make_history(max_turns=6)
        for number in range(1, 9):
            history.add_turn(f"pregunta {number}", f"respuesta {number}")

        messages = history.to_messages_list(pending_user_content="pregunta 9")

        self.assertEqual(len(user_messages(messages)), 6)
        self.assertEqual(
            user_messages(messages),
            ["pregunta 4", "pregunta 5", "pregunta 6", "pregunta 7", "pregunta 8", "pregunta 9"],
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1], {"role": "user", "content": "pregunta 9"})

    def test_window_of_one_turn_sends_only_the_new_question(self):
        # Caso límite: lista[-0:] devolvería todo; aquí no debe colarse nada antiguo.
        history = self.make_history(max_turns=1)
        history.add_turn("vieja", "respuesta vieja")

        messages = history.to_messages_list(pending_user_content="nueva")

        self.assertEqual(
            messages,
            [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "nueva"}],
        )

    def test_system_prompt_is_regenerated_on_every_call(self):
        calls = []

        def factory():
            calls.append(1)
            return f"SYSTEM v{len(calls)}"

        history = ConversationHistory(system_prompt_factory=factory)

        self.assertEqual(history.to_messages_list()[0]["content"], "SYSTEM v1")
        self.assertEqual(history.to_messages_list()[0]["content"], "SYSTEM v2")

    def test_content_blocks_are_accepted_for_attachments(self):
        history = self.make_history()
        blocks = [
            {"type": "document", "source": {"type": "file", "file_id": "file_123"}},
            {"type": "text", "text": "Estima con este documento"},
        ]
        history.add_turn(blocks, "respuesta")

        self.assertEqual(history.to_messages_list()[1]["content"], blocks)

    def test_messages_property_returns_a_copy(self):
        history = self.make_history(max_turns=1)
        history.add_turn("pregunta", "respuesta")

        history.messages.append({"role": "user", "content": "colado"})
        history.messages[0]["content"] = "modificado"

        self.assertEqual(history.turn_count, 1)
        self.assertEqual(history.messages[0]["content"], "pregunta")


class SessionTests(unittest.TestCase):
    def test_new_session_starts_with_empty_memory_and_history(self):
        session = Session("abc", fake_prompt_builder)

        self.assertEqual(session.session_id, "abc")
        self.assertTrue(session.metadata.is_empty())
        self.assertEqual(session.history.turn_count, 0)

    def test_system_prompt_follows_the_current_metadata(self):
        session = Session("abc", fake_prompt_builder)
        self.assertEqual(
            session.history.to_messages_list()[0]["content"], "SYSTEM | proyecto=None"
        )

        # Cambiar un campo o sustituir la ficha entera se ve en la siguiente llamada.
        session.metadata.project_name = "Portal de reservas"
        self.assertIn("Portal de reservas", session.history.to_messages_list()[0]["content"])

        session.metadata = ProjectMetadata(project_name="CRM interno")
        self.assertIn("CRM interno", session.history.to_messages_list()[0]["content"])

    def test_memory_survives_when_the_turn_leaves_the_window(self):
        session = Session("abc", fake_prompt_builder, max_turns=1)
        session.history.add_turn("Se llama Portal de reservas", "Anotado")
        session.metadata.project_name = "Portal de reservas"
        session.history.add_turn("Otra cosa", "Vale")

        messages = session.history.to_messages_list()

        # El turno donde se dijo el nombre ya no está en el historial...
        self.assertNotIn("Se llama Portal de reservas", user_messages(messages))
        # ...pero el dato sigue llegando al modelo a través del system prompt.
        self.assertIn("Portal de reservas", messages[0]["content"])


class SessionStoreTests(unittest.TestCase):
    def test_create_returns_an_empty_session_with_uuid4_id(self):
        store = SessionStore(fake_prompt_builder)

        session = store.create()

        self.assertEqual(uuid.UUID(session.session_id).version, 4)
        self.assertIn(session.session_id, store)
        self.assertEqual(len(store), 1)
        self.assertTrue(session.metadata.is_empty())

    def test_get_returns_the_same_session_object(self):
        store = SessionStore(fake_prompt_builder)
        session = store.create()
        session.metadata.project_name = "CRM"

        again = store.get(session.session_id)

        self.assertIs(again, session)
        self.assertEqual(again.metadata.project_name, "CRM")

    def test_unknown_session_raises_a_clear_error(self):
        store = SessionStore(fake_prompt_builder)

        with self.assertRaises(SessionNotFoundError):
            store.get("no-existe")

    def test_sessions_inherit_the_store_window(self):
        store = SessionStore(fake_prompt_builder, max_turns=2)

        self.assertEqual(store.create().history.max_turns, 2)

    def test_least_recently_used_session_is_evicted_when_full(self):
        store = SessionStore(fake_prompt_builder, max_sessions=2)
        first = store.create()
        second = store.create()

        store.get(first.session_id)  # ahora la menos usada es "second"
        third = store.create()

        self.assertEqual(len(store), 2)
        self.assertIn(first.session_id, store)
        self.assertNotIn(second.session_id, store)
        self.assertIn(third.session_id, store)

    def test_delete_removes_the_session_and_ignores_unknown_ids(self):
        store = SessionStore(fake_prompt_builder)
        session = store.create()

        store.delete(session.session_id)
        store.delete("no-existe")

        self.assertNotIn(session.session_id, store)
        self.assertEqual(len(store), 0)


class SessionTtlTests(unittest.TestCase):
    """Paso 01.4: caducidad por inactividad (TTL), archivado y memoria heredada."""

    def setUp(self):
        self.clock = FakeClock()
        self.store = SessionStore(fake_prompt_builder, ttl_seconds=3600, clock=self.clock)

    def test_a_session_used_within_the_hour_stays_alive(self):
        session = self.store.create()

        self.clock.advance(minutes=59)

        self.assertIs(self.store.get(session.session_id), session)

    def test_activity_restarts_the_countdown(self):
        """Usarla a los 50 minutos le da otra hora entera."""
        session = self.store.create()
        self.clock.advance(minutes=50)
        self.store.get(session.session_id)

        self.clock.advance(minutes=50)  # 100 minutos desde que se creó

        self.assertIs(self.store.get(session.session_id), session)
        self.assertEqual(self.store.expires_at(session), self.clock.now + timedelta(hours=1))

    def test_after_an_hour_without_activity_it_expires_and_keeps_only_its_memory(self):
        session = self.store.create()
        session.metadata = ProjectMetadata(project_name="Portal", assumed_team_size=3)
        session.history.add_turn("hola", "respuesta")

        self.clock.advance(minutes=61)
        with self.assertRaises(SessionExpiredError) as context:
            self.store.get(session.session_id)

        archived = context.exception.archived
        self.assertEqual(archived.metadata.project_name, "Portal")
        self.assertEqual(archived.archived_at, self.clock.now)
        self.assertNotIn(session.session_id, self.store)  # ya no está entre las vivas
        # Pedirla otra vez sigue diciendo "caducada" (no "no existe").
        with self.assertRaises(SessionExpiredError):
            self.store.get(session.session_id)

    def test_creating_a_session_archives_the_expired_ones(self):
        """Limpieza "perezosa": no hace falta que nadie vuelva a pedir la vieja."""
        old = self.store.create()
        self.clock.advance(hours=2)

        self.store.create()

        self.assertEqual(len(self.store), 1)
        self.assertIsNotNone(self.store.get_archived(old.session_id))

    def test_new_session_can_inherit_the_memory_of_an_expired_one(self):
        old = self.store.create()
        old.metadata = ProjectMetadata(project_name="Portal", mentioned_technologies=["React"])
        old.history.add_turn("hola", "respuesta")
        self.clock.advance(hours=2)

        new = self.store.create(inherit_from=old.session_id)

        self.assertNotEqual(new.session_id, old.session_id)
        self.assertEqual(new.inherited_from, old.session_id)
        self.assertEqual(new.metadata.project_name, "Portal")
        self.assertEqual(new.history.turn_count, 0)  # el historial NO se hereda
        # Es una copia: cambiar la nueva no toca la archivada.
        new.metadata = ProjectMetadata(project_name="Otro")
        self.assertEqual(self.store.get_archived(old.session_id).metadata.project_name, "Portal")

    def test_inheriting_from_an_unknown_session_fails(self):
        with self.assertRaises(SessionNotFoundError):
            self.store.create(inherit_from=str(uuid.uuid4()))

    def test_the_archive_is_also_capped(self):
        store = SessionStore(fake_prompt_builder, max_sessions=2, ttl_seconds=60, clock=self.clock)
        ids = []
        for _ in range(4):
            ids.append(store.create().session_id)
            self.clock.advance(minutes=2)
        store.create()  # archiva las caducadas

        self.assertIsNone(store.get_archived(ids[0]))  # la más antigua ya no cabe
        self.assertIsNotNone(store.get_archived(ids[-1]))


class MemoryCapsTests(unittest.TestCase):
    """La ficha viaja en cada system prompt: no puede crecer sin control."""

    def test_very_long_texts_are_cut(self):
        metadata = ProjectMetadata(project_name="x" * 5000, agreed_scope="y" * 5000)

        self.assertEqual(len(metadata.project_name), MAX_FACT_CHARS)
        self.assertEqual(len(metadata.agreed_scope), MAX_FACT_CHARS)

    def test_technology_list_has_a_ceiling_and_keeps_the_first_ones(self):
        names = [f"tec{i}" for i in range(500)] + ["una frase larguísima que no es ninguna tecnología" * 2]

        metadata = ProjectMetadata(mentioned_technologies=names)

        self.assertEqual(len(metadata.mentioned_technologies), MAX_TECHNOLOGIES)
        self.assertEqual(metadata.mentioned_technologies[0], "tec0")

    def test_absurd_team_sizes_are_ignored(self):
        self.assertIsNone(ProjectMetadata(assumed_team_size=50_000).assumed_team_size)


if __name__ == "__main__":
    unittest.main()
