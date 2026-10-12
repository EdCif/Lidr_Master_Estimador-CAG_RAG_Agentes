"""Pruebas HTTP de los endpoints de sesiones, sin LLM ni claves.

Las llamadas al LLM se sustituyen por generadores de prueba: así comprobamos
el recorrido completo (formulario multipart -> validación -> servicio ->
streaming SSE -> historial) sin gastar API.
"""

import json
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import get_settings
from app.errors import install_error_handlers
from app.routers.sessions import get_session_store, router
from app.services import conversation_service
from app.services.attachments.common import LLMProviderError
from app.services.sessions import ProjectMetadata, SessionStore
from doc_factories import make_docx, make_text_pdf


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Convierte el texto de un stream SSE en una lista de (evento, datos).

    En el protocolo SSE cada evento son unas líneas "event: ..." y
    "data: ..." separadas del siguiente por una línea en blanco. Las líneas
    que empiezan por ":" son comentarios (los pings de FastAPI) y se ignoran.
    """
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        name, data_lines = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


def fake_settings(**overrides):
    """Los ajustes que leen los endpoints, sin tocar el .env real."""
    values = dict(
        anthropic_api_key=SecretStr("test-key-not-a-real-credential"),
        session_llm_model="claude-opus-5-5",
        session_llm_effort="medium",
        session_max_tokens=64000,
        attachment_mode="local",
        pdf_extractor="pypdf",
        max_attachments=5,
        max_attachment_mb=10,
        max_attachment_chars=300_000,
        max_pdf_pages=300,
        max_docx_uncompressed_mb=50,
        attachment_extraction_timeout_s=30,
        max_turns=6,
        max_sessions=500,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


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

    def test_store_takes_window_size_and_session_limit_from_settings(self):
        """MAX_TURNS y MAX_SESSIONS del .env llegan al almacén real de la API.

        get_session_store() guarda su resultado (lru_cache), así que vaciamos
        esa caché antes y después para no contaminar otras pruebas.
        """
        settings = fake_settings(max_turns=3, max_sessions=7)
        get_session_store.cache_clear()
        self.addCleanup(get_session_store.cache_clear)
        with patch("app.routers.sessions.get_settings", return_value=settings):
            store = get_session_store()

        self.assertEqual((store.max_turns, store.max_sessions), (3, 7))
        session = store.create()
        for number in range(1, 6):
            session.history.add_turn(f"pregunta {number}", f"respuesta {number}")
        # Con ventana 3: solo quedan los 3 últimos turnos; los 2 primeros se descartan.
        self.assertEqual(session.history.turn_count, 3)
        self.assertEqual(session.history.discarded_turns, 2)

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


class EstimateEndpointTests(unittest.TestCase):
    """POST /sessions/{id}/estimate: formulario multipart y respuesta SSE."""

    def setUp(self):
        self.store = SessionStore(system_prompt_builder=lambda metadata: "SYSTEM")
        self.settings = fake_settings()
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(router)
        app.dependency_overrides[get_session_store] = lambda: self.store
        app.dependency_overrides[get_settings] = lambda: self.settings
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.session_id = self.client.post("/sessions").json()["session_id"]

        # Camino B de mentira: graba los mensajes que recibe y contesta fijo.
        self.sent_messages = []
        self.reply_chunks = ["Estimación: ", "80 horas"]
        self.reply_error = None

        async def fake_local_reply(messages, *, model, api_key, max_tokens, stats):
            self.sent_messages.append(messages)
            for chunk in self.reply_chunks:
                yield chunk
            if self.reply_error:
                raise self.reply_error
            stats.final_text = "".join(self.reply_chunks)
            stats.input_tokens, stats.output_tokens = 100, 10

        local_patch = patch.object(conversation_service.path_b_local, "stream_reply", fake_local_reply)
        local_patch.start()
        self.addCleanup(local_patch.stop)

    def estimate(self, transcript="Necesitamos un portal de reservas.", files=None, **form):
        response = self.client.post(
            f"/sessions/{self.session_id}/estimate",
            data={"transcript": transcript, **form},
            files=files,
        )
        return response

    def test_transcript_only_streams_status_deltas_and_done(self):
        response = self.estimate()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = parse_sse(response.text)
        names = [name for name, _ in events]
        self.assertEqual(names[0], "status")
        self.assertEqual([data["text"] for name, data in events if name == "delta"], self.reply_chunks)
        self.assertEqual(names[-1], "done")
        done = events[-1][1]
        self.assertEqual(done["estimation"], "Estimación: 80 horas")
        self.assertEqual(done["attachment_mode"], "local")
        self.assertEqual(done["session"]["turn_count"], 1)
        self.assertEqual(done["usage"], {"input_tokens": 100, "output_tokens": 10})

    def test_turn_is_saved_in_history_and_sent_on_the_next_call(self):
        self.estimate("Primera petición")
        self.estimate("Ahora sin app móvil")

        second_call = self.sent_messages[1]
        self.assertEqual(
            [(m["role"], m["content"]) for m in second_call],
            [
                ("system", "SYSTEM"),
                ("user", "Primera petición"),
                ("assistant", "Estimación: 80 horas"),
                ("user", "Ahora sin app móvil"),
            ],
        )
        self.assertEqual(self.store.get(self.session_id).history.turn_count, 2)

    def test_path_b_appends_attachment_text_with_separator(self):
        docx = make_docx("Especificación", ["Usaremos Kubernetes y PostgreSQL."])
        pdf = make_text_pdf("Diagrama: API Gateway y Kafka")
        files = [
            ("attachments", ("spec.docx", docx, "application/octet-stream")),
            ("attachments", ("arquitectura.pdf", pdf, "application/pdf")),
        ]

        events = parse_sse(self.estimate(files=files).text)

        user_message = self.sent_messages[0][-1]["content"]
        self.assertTrue(user_message.startswith("Necesitamos un portal de reservas."))
        self.assertIn("--- attachment: spec.docx ---\n# Especificación", user_message)
        self.assertIn("Kubernetes", user_message)
        self.assertIn("--- attachment: arquitectura.pdf ---", user_message)
        self.assertIn("Kafka", user_message)
        notes = [data["message"] for name, data in events if name == "status"]
        self.assertTrue(any("python-docx" in note for note in notes))
        self.assertTrue(any("pypdf" in note for note in notes))

    def test_path_a_uploads_pdf_and_sends_document_blocks(self):
        uploads, stream_calls = [], []

        class FakeStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            @property
            def text_stream(self):
                async def chunks():
                    yield "Estimación con el PDF"
                return chunks()

            async def get_final_message(self):
                return SimpleNamespace(
                    model="claude-opus-5-5",
                    stop_reason="end_turn",
                    usage=SimpleNamespace(input_tokens=900, output_tokens=5),
                    content=[SimpleNamespace(type="text", text="Estimación con el PDF")],
                )

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            files = SimpleNamespace(upload=None)
            beta = SimpleNamespace(messages=SimpleNamespace(stream=None))

        async def upload(*, file, expires_in_seconds):
            uploads.append(file)
            return SimpleNamespace(id="file_abc")

        def stream(**kwargs):
            stream_calls.append(kwargs)
            return FakeStream()

        FakeClient.files.upload = upload
        FakeClient.beta.messages.stream = stream
        files = [("attachments", ("arquitectura.pdf", make_text_pdf("Kafka"), "application/pdf"))]

        with patch.object(conversation_service.path_a_files_api, "make_client", lambda api_key: FakeClient()):
            response = self.estimate(files=files, attachment_mode="files_api")

        events = parse_sse(response.text)
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1]["attachment_mode"], "files_api")
        self.assertEqual(uploads[0][0], "arquitectura.pdf")
        sent_user = stream_calls[0]["messages"][-1]["content"]
        self.assertEqual(sent_user[0]["source"], {"type": "file", "file_id": "file_abc"})
        self.assertEqual(sent_user[-1], {"type": "text", "text": "Necesitamos un portal de reservas."})
        self.assertEqual(stream_calls[0]["system"], "SYSTEM")
        state = self.client.get(f"/sessions/{self.session_id}").json()
        self.assertEqual(state["uploaded_files"], [{"filename": "arquitectura.pdf", "file_id": "file_abc"}])

    def test_provider_failure_sends_error_event_and_does_not_save_the_turn(self):
        self.reply_error = LLMProviderError("No se ha podido conectar con el proveedor del LLM. Reintenta.")

        events = parse_sse(self.estimate().text)

        self.assertEqual(events[-1], ("error", {"detail": "No se ha podido conectar con el proveedor del LLM. Reintenta."}))
        self.assertEqual(self.store.get(self.session_id).history.turn_count, 0)

    def test_pdf_without_text_in_path_b_ends_with_error_event(self):
        from doc_factories import make_blank_pdf

        files = [("attachments", ("escaneado.pdf", make_blank_pdf(), "application/pdf"))]
        events = parse_sse(self.estimate(files=files).text)

        self.assertEqual(events[-1][0], "error")
        self.assertIn("files_api", events[-1][1]["detail"])
        self.assertEqual(self.sent_messages, [])

    def test_errors_detected_before_streaming_return_real_http_codes(self):
        too_many = [("attachments", (f"a{i}.pdf", make_text_pdf("x"), "application/pdf")) for i in range(6)]
        cases = {
            "sesión inexistente": (lambda: self.client.post(
                f"/sessions/{uuid.uuid4()}/estimate", data={"transcript": "Hola"}), 404),
            "transcripción vacía": (lambda: self.estimate("   "), 422),
            "sin campo transcript": (lambda: self.client.post(f"/sessions/{self.session_id}/estimate"), 422),
            "fichero .txt": (lambda: self.estimate(files=[("attachments", ("notas.txt", b"hola", "text/plain"))]), 415),
            "demasiados adjuntos": (lambda: self.estimate(files=too_many), 413),
            "camino desconocido": (lambda: self.estimate(attachment_mode="camino_c"), 422),
        }
        for label, (call, expected_status) in cases.items():
            with self.subTest(label):
                self.assertEqual(call().status_code, expected_status)
        self.assertEqual(self.sent_messages, [])

    def test_too_many_pdf_pages_or_inflated_docx_return_413_before_streaming(self):
        """Los topes de páginas y de Word descomprimido salen como 413 de verdad.

        Se comprueban en la dependencia, antes de abrir el stream, así que el
        cliente recibe el código HTTP correcto y el LLM no llega a llamarse.
        """
        from doc_factories import make_blank_pdf

        self.settings.max_pdf_pages = 2
        self.settings.max_docx_uncompressed_mb = 0.001  # ~1 KB: cualquier Word real se pasa
        cases = {
            "PDF de 3 páginas": ("largo.pdf", make_blank_pdf(pages=3), "application/pdf"),
            "Word inflado": ("grande.docx", make_docx("T", ["p"]), "application/octet-stream"),
        }
        for label, upload in cases.items():
            with self.subTest(label):
                response = self.estimate(files=[("attachments", upload)])
                self.assertEqual(response.status_code, 413)
        self.assertEqual(self.sent_messages, [])

    def test_missing_anthropic_key_returns_503(self):
        self.settings.anthropic_api_key = None

        response = self.estimate()

        self.assertEqual(response.status_code, 503)
        self.assertIn("ANTHROPIC_API_KEY", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
