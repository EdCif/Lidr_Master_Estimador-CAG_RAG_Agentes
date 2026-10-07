"""Pruebas HTTP de los endpoints de sesiones, sin LLM ni claves."""

import unittest
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import install_error_handlers
from app.routers.sessions import get_session_store, router
from app.services.sessions import ProjectMetadata, SessionStore


class SessionsApiTests(unittest.TestCase):
    def setUp(self):
        # Montar solo el router evita cargar la configuración real de main.py.
        # Cada prueba usa un almacén nuevo para no arrastrar sesiones de otra.
        self.store = SessionStore(system_prompt_builder=lambda metadata: "SYSTEM")
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(router)
        app.dependency_overrides[get_session_store] = lambda: self.store
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_post_sessions_creates_an_empty_session_and_returns_its_id(self):
        response = self.client.post("/sessions")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(list(response.json()), ["session_id"])
        session_id = response.json()["session_id"]
        self.assertEqual(uuid.UUID(session_id).version, 4)
        self.assertIn(session_id, self.store)
        self.assertTrue(self.store.get(session_id).metadata.is_empty())

    def test_each_call_creates_a_different_session(self):
        first = self.client.post("/sessions").json()["session_id"]
        second = self.client.post("/sessions").json()["session_id"]

        self.assertNotEqual(first, second)
        self.assertEqual(len(self.store), 2)

    def test_get_session_shows_memory_and_history_separately(self):
        session_id = self.client.post("/sessions").json()["session_id"]
        session = self.store.get(session_id)
        session.history.add_turn("Se llama Portal de reservas", "Anotado")
        session.metadata = ProjectMetadata(project_name="Portal de reservas")

        response = self.client.get(f"/sessions/{session_id}")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["session_id"], session_id)
        self.assertEqual(body["max_turns"], 6)
        self.assertEqual(body["turn_count"], 1)
        self.assertEqual(body["discarded_turns"], 0)
        self.assertEqual(body["project_metadata"]["project_name"], "Portal de reservas")
        self.assertEqual(
            body["history"],
            [
                {"role": "user", "content": "Se llama Portal de reservas"},
                {"role": "assistant", "content": "Anotado"},
            ],
        )

    def test_session_id_in_uppercase_finds_the_same_session(self):
        session_id = self.client.post("/sessions").json()["session_id"]

        response = self.client.get(f"/sessions/{session_id.upper()}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], session_id)

    def test_unknown_session_returns_404_with_a_hint(self):
        response = self.client.get(f"/sessions/{uuid.uuid4()}")

        self.assertEqual(response.status_code, 404)
        self.assertIn("POST /sessions", response.json()["detail"])

    def test_malformed_session_id_is_rejected_with_422(self):
        response = self.client.get("/sessions/no-es-un-uuid")

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
