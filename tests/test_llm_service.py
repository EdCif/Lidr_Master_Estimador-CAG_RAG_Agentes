"""Pruebas del servicio de estimación sin credenciales ni llamadas de red."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai import OpenAIError
from pydantic import SecretStr

from app.context.examples import ESTIMATION_EXAMPLES
from app.services import llm_service


def make_response(content="Estimación de prueba", finish_reason="stop", refusal=None):
    """Construye solo los campos del SDK que consume el servicio."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, refusal=refusal),
            )
        ]
    )


class SystemPromptTests(unittest.TestCase):
    def test_format_examples_preserves_all_data_and_accents(self):
        examples = [
            {"petición": "Gestión de tesorería", "tareas": [{"horas": 8}]},
            {"exclusiones": ["Importación masiva"], "total": 16},
        ]

        formatted = llm_service.format_examples(examples)

        self.assertEqual(json.loads(formatted), examples)
        self.assertIn("Gestión de tesorería", formatted)
        self.assertIn("Importación masiva", formatted)

    def test_system_prompt_includes_complete_reference_examples(self):
        prompt = llm_service.build_system_prompt()
        examples_text = json.dumps(ESTIMATION_EXAMPLES, ensure_ascii=False, indent=2)

        self.assertIn(examples_text, prompt)
        self.assertGreater(len(prompt), len(examples_text))


class GenerateEstimationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            openai_api_key=SecretStr("test-key-not-a-real-credential"),
            llm_model="configured-test-model",
            llm_provider=" OpenAI ",
        )
        settings_patch = patch.object(llm_service, "get_settings", return_value=self.settings)
        self.get_settings = settings_patch.start()
        self.addCleanup(settings_patch.stop)

        client_patch = patch.object(llm_service, "AsyncOpenAI")
        self.client_factory = client_patch.start()
        self.addCleanup(client_patch.stop)
        self.client_context = self.client_factory.return_value
        self.create_completion = AsyncMock(return_value=make_response())
        self.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=self.create_completion)
            )
        )
        self.client_context.__aenter__.return_value = self.client
        self.client_context.__aexit__.return_value = False

    async def test_request_uses_configured_model_key_and_message_roles(self):
        transcription = "Necesitamos una aplicación para gestionar reservas."
        self.create_completion.return_value = make_response("  Estimación: 80 horas.\n")

        result = await llm_service.generate_estimation(transcription)

        self.client_factory.assert_called_once_with(
            api_key="test-key-not-a-real-credential", timeout=60.0
        )
        self.create_completion.assert_awaited_once_with(
            model="configured-test-model",
            messages=[
                {"role": "system", "content": llm_service.build_system_prompt()},
                {"role": "user", "content": transcription},
            ],
            max_completion_tokens=2000,
            store=False,
        )
        self.assertEqual(
            result,
            {
                "estimation": "Estimación: 80 horas.",
                "model": "configured-test-model",
                "provider": "openai",
            },
        )
        self.client_context.__aenter__.assert_awaited_once()
        self.client_context.__aexit__.assert_awaited_once()

    async def test_invalid_transcription_does_not_create_client(self):
        for transcription in (None, 42, [], "", " \n\t"):
            with self.subTest(transcription=transcription):
                with self.assertRaises(ValueError):
                    await llm_service.generate_estimation(transcription)

        self.client_factory.assert_not_called()

    async def test_unsupported_provider_does_not_create_client(self):
        self.settings.llm_provider = "another-provider"

        with self.assertRaises(ValueError):
            await llm_service.generate_estimation("Crear una aplicación de reservas.")

        self.client_factory.assert_not_called()

    async def test_invalid_responses_raise_and_close_client(self):
        responses = {
            "no_choices": SimpleNamespace(choices=[]),
            "refusal": make_response(refusal="No puedo responder a esta petición."),
            "truncated": make_response(finish_reason="length"),
            "filtered": make_response(finish_reason="content_filter"),
            "missing_content": make_response(content=None),
            "empty_content": make_response(content=""),
            "blank_content": make_response(content=" \n\t"),
        }

        for label, response in responses.items():
            with self.subTest(response=label):
                self.client_context.__aexit__.reset_mock()
                self.create_completion.return_value = response

                with self.assertRaises(RuntimeError):
                    await llm_service.generate_estimation("Crear una aplicación de reservas.")

                self.client_context.__aexit__.assert_awaited_once()

    async def test_sdk_error_propagates_and_closes_client(self):
        error = OpenAIError("Fallo simulado del proveedor")
        self.create_completion.side_effect = error

        with self.assertRaises(OpenAIError) as raised:
            await llm_service.generate_estimation("Crear una aplicación de reservas.")

        self.assertIs(raised.exception, error)
        self.client_context.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
