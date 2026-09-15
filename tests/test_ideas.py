"""Pruebas de persistencia y flujo HTTP, con SQLite temporal y LLM simulado."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import install_error_handlers
from app.routers.ideas import get_data_dir, router
from app.services import idea_service as store


class IdeasTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        store.init_store(self.data_dir)
        self.app = FastAPI()
        install_error_handlers(self.app)
        self.app.include_router(router)
        self.app.dependency_overrides[get_data_dir] = lambda: self.data_dir
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        settings_patch = patch("app.routers.ideas.get_settings", side_effect=AssertionError("No usar .env"))
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.result = {"estimation": "Desarrollo: 24 horas.", "model": "test-model", "provider": "openai"}
        provider_patch = patch("app.routers.ideas.generate_estimation", new_callable=AsyncMock,
                               return_value=self.result)
        self.generate = provider_patch.start()
        self.addCleanup(provider_patch.stop)

    def create(self, transcription="Crear una agenda para reservas."):
        response = self.client.post("/api/v1/ideas", json={"title": "Agenda", "transcription": transcription})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_create_list_update_and_read_survive_reopening_store(self):
        idea = self.create()
        self.assertEqual(idea["status"], "draft")
        self.assertEqual(idea["runs"], [])
        self.assertIsNone(idea["latest_run"])
        update = {"title": "Agenda MVP", "notes": "Cliente: 'OK'; DROP TABLE ideas;", "status": "planned"}
        response = self.client.patch(f"/api/v1/ideas/{idea['id']}", json=update)
        self.assertEqual(response.status_code, 200)
        store.init_store(self.data_dir)
        restored = self.client.get(f"/api/v1/ideas/{idea['id']}").json()
        for key, value in update.items():
            self.assertEqual(restored[key], value)
        self.assertEqual(restored["transcription"], idea["transcription"])
        summary = self.client.get("/api/v1/ideas").json()[0]
        self.assertEqual(summary["id"], idea["id"])
        self.assertNotIn("runs", summary)
        self.assertEqual(summary["run_count"], 0)
        self.generate.assert_not_awaited()

    def test_estimate_tracks_result_and_keeps_previous_transcription(self):
        idea = self.create()
        path = f"/api/v1/ideas/{idea['id']}"
        first = self.client.post(f"{path}/estimate")
        self.assertEqual(first.status_code, 200, first.text)
        first_run = first.json()["latest_run"]
        self.assertEqual(first_run["state"], "completed")
        self.assertIsNone(first_run["error"])
        self.assertIsNotNone(first_run["finished_at"])
        for key, value in self.result.items():
            self.assertEqual(first_run[key], value)
        self.generate.assert_awaited_once_with(idea["transcription"])
        self.client.patch(path, json={"transcription": "Añadir un calendario compartido.", "status": "in_review"})
        second = self.client.post(f"{path}/estimate").json()
        self.assertEqual(second["run_count"], 2)
        self.assertEqual(second["runs"][1]["transcription"], idea["transcription"])
        self.assertEqual(second["runs"][1]["id"], first_run["id"])
        self.assertEqual(second["latest_run"]["transcription"], "Añadir un calendario compartido.")
        self.assertEqual(second["status"], "in_review")

    def test_failed_attempt_is_sanitized_and_can_be_retried(self):
        idea = self.create()
        path = f"/api/v1/ideas/{idea['id']}"
        secret_detail = "sk-should-never-be-stored"
        self.generate.side_effect = RuntimeError(secret_detail)
        response = self.client.post(f"{path}/estimate")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(secret_detail, response.text)
        detail = self.client.get(path)
        self.assertNotIn(secret_detail, detail.text)
        failed = detail.json()["latest_run"]
        self.assertEqual(failed["state"], "failed")
        self.assertIsNotNone(failed["finished_at"])
        self.assertTrue(failed["error"])
        self.assertIsNone(failed["estimation"])
        self.generate.side_effect = None
        retried = self.client.post(f"{path}/estimate")
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["run_count"], 2)

    def test_request_validation_and_missing_ideas_do_not_call_provider(self):
        for payload in ({}, {"title": "  "}, {"title": "A", "notes": None}, {"title": "A", "unknown": 1}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/v1/ideas", json=payload).status_code, 422)
        idea = self.create(transcription="")
        path = f"/api/v1/ideas/{idea['id']}"
        for payload in ({"status": "unknown"}, {"title": " "}, {"notes": None}, {"id": "change-id"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.patch(path, json=payload).status_code, 422)
        self.assertEqual(self.client.post(f"{path}/estimate").status_code, 422)
        self.assertEqual(self.client.get(path).json()["run_count"], 0)
        self.assertEqual(self.client.get("/api/v1/ideas/missing").status_code, 404)
        self.assertEqual(self.client.patch("/api/v1/ideas/missing", json={"notes": "test"}).status_code, 404)
        self.assertEqual(self.client.post("/api/v1/ideas/missing/estimate").status_code, 404)
        self.generate.assert_not_awaited()

    def test_concurrent_estimation_is_rejected_and_snapshot_survives_edit(self):
        idea = self.create()
        path = f"/api/v1/ideas/{idea['id']}"

        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()

            async def provider(transcription):
                entered.set()
                await release.wait()
                return self.result

            self.generate.side_effect = provider
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                first = asyncio.create_task(client.post(f"{path}/estimate"))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    current = (await client.get(path)).json()
                    self.assertEqual(current["latest_run"]["state"], "running")
                    second = await client.post(f"{path}/estimate")
                    self.assertEqual(second.status_code, 409)
                    await client.patch(path, json={"transcription": "Nuevos requisitos mientras estima."})
                finally:
                    release.set()
                response = await first
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["latest_run"]["transcription"], idea["transcription"])
                self.assertEqual(response.json()["transcription"], "Nuevos requisitos mientras estima.")
                self.generate.assert_awaited_once_with(idea["transcription"])

        asyncio.run(scenario())

    def test_recovery_closes_interrupted_runs_without_losing_history(self):
        idea = self.create()
        run = store.start_run(self.data_dir, idea["id"])
        store.recover_running_runs(self.data_dir)
        recovered = store.get_idea(self.data_dir, idea["id"])
        self.assertEqual(recovered["latest_run"]["id"], run["id"])
        self.assertEqual(recovered["latest_run"]["state"], "failed")
        self.assertIn("reinició", recovered["latest_run"]["error"])
        self.assertEqual(self.client.post(f"/api/v1/ideas/{idea['id']}/estimate").status_code, 200)
        store.recover_running_runs(self.data_dir)
        self.assertEqual(store.get_idea(self.data_dir, idea["id"])["latest_run"]["state"], "completed")

    def test_incomplete_provider_response_is_recorded_as_failure(self):
        idea = self.create()
        self.generate.return_value = {"estimation": "", "model": "test", "provider": "openai"}
        path = f"/api/v1/ideas/{idea['id']}"
        self.assertEqual(self.client.post(f"{path}/estimate").status_code, 500)
        self.assertEqual(self.client.get(path).json()["latest_run"]["state"], "failed")


if __name__ == "__main__":
    unittest.main()
