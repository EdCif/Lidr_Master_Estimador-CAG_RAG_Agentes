"""Pruebas HTTP de estimaciones, sin credenciales ni llamadas a OpenAI."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openai import APITimeoutError, OpenAIError, RateLimitError

from app.errors import install_error_handlers
from app.routers.estimations import router


class EstimationsRouterTests(unittest.TestCase):
    def setUp(self):
        # Montar solo el router evita cargar la configuración real de main.py.
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(router)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.result = {
            "estimation": "Estimación de prueba: 80 horas.",
            "model": "configured-test-model",
            "provider": "openai",
        }
        service_patch = patch(
            "app.routers.estimations.generate_estimation",
            new_callable=AsyncMock,
            return_value=self.result,
        )
        self.generate_estimation = service_patch.start()
        self.addCleanup(service_patch.stop)

    def test_valid_request_returns_estimation_and_strips_transcription(self):
        response = self.client.post(
            "/api/v1/estimate",
            json={"transcription": "  Necesitamos gestionar reservas.\n"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.result)
        self.generate_estimation.assert_awaited_once_with(
            "Necesitamos gestionar reservas."
        )

    def test_invalid_requests_are_rejected_before_calling_service(self):
        invalid_payloads = [
            {},
            {"transcription": None},
            {"transcription": ""},
            {"transcription": " \n\t"},
            {"transcription": 42},
            {"transcription": True},
            {"transcription": []},
            {"transcription": {}},
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/api/v1/estimate", json=payload)

                self.assertEqual(response.status_code, 422)
                self.generate_estimation.assert_not_awaited()

    def test_response_model_filters_extra_service_fields(self):
        self.generate_estimation.return_value = {
            **self.result,
            "clave_ficticia": "dato-que-no-debe-salir-en-la-respuesta",
        }

        response = self.client.post(
            "/api/v1/estimate", json={"transcription": "Crear una API."}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.result)

    def test_invalid_service_responses_are_not_returned_as_success(self):
        invalid_results = [
            {},
            {**self.result, "estimation": ""},
            {**self.result, "model": None},
            {**self.result, "provider": ""},
        ]

        for result in invalid_results:
            with self.subTest(result=result):
                self.generate_estimation.return_value = result

                response = self.client.post(
                    "/api/v1/estimate", json={"transcription": "Crear una API."}
                )

                self.assertEqual(response.status_code, 500)

    def test_service_errors_map_to_http_errors_without_internal_details(self):
        internal_detail = "detalle-interno-de-prueba-no-publicar"
        request = httpx.Request("POST", "https://example.invalid/completions")
        errors = [
            (APITimeoutError(request=request), 504),
            (
                RateLimitError(
                    internal_detail,
                    response=httpx.Response(429, request=request),
                    body=None,
                ),
                503,
            ),
            (OpenAIError(internal_detail), 502),
            (RuntimeError(internal_detail), 502),
            (ValueError(internal_detail), 500),
        ]

        for error, expected_status in errors:
            with self.subTest(error=type(error).__name__):
                self.generate_estimation.reset_mock()
                self.generate_estimation.side_effect = error

                response = self.client.post(
                    "/api/v1/estimate", json={"transcription": "Crear una API."}
                )

                self.assertEqual(response.status_code, expected_status)
                self.assertIsInstance(response.json()["detail"], str)
                self.assertTrue(response.json()["detail"])
                self.assertNotIn(internal_detail, response.text)
                self.assertNotIn(str(error), response.text)
                self.generate_estimation.assert_awaited_once_with("Crear una API.")

    def test_get_is_not_allowed(self):
        response = self.client.get("/api/v1/estimate")

        self.assertEqual(response.status_code, 405)
        self.generate_estimation.assert_not_awaited()

    def test_unknown_route_keeps_not_found_response(self):
        response = self.client.get("/unknown")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Not Found"})
        self.generate_estimation.assert_not_awaited()

    def test_http_exception_keeps_status_detail_and_headers(self):
        self.generate_estimation.side_effect = HTTPException(
            status_code=409,
            detail="La estimación ya está en curso.",
            headers={"Retry-After": "5"},
        )

        response = self.client.post(
            "/api/v1/estimate", json={"transcription": "Crear una API."}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "La estimación ya está en curso."})
        self.assertEqual(response.headers["Retry-After"], "5")

    def test_openapi_describes_request_and_response_contracts(self):
        response = self.client.get("/openapi.json")

        self.assertEqual(response.status_code, 200)
        specification = response.json()
        operation = specification["paths"]["/api/v1/estimate"]["post"]
        self.assertTrue(operation["requestBody"]["required"])
        request_schema = operation["requestBody"]["content"]["application/json"][
            "schema"
        ]
        response_schema = operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        self.assertEqual(
            request_schema["$ref"], "#/components/schemas/EstimationRequest"
        )
        self.assertEqual(
            response_schema["$ref"], "#/components/schemas/EstimationResponse"
        )
        schemas = specification["components"]["schemas"]
        self.assertIn("transcription", schemas["EstimationRequest"]["required"])
        self.assertEqual(
            schemas["EstimationRequest"]["properties"]["transcription"]["minLength"],
            1,
        )
        self.assertEqual(
            set(schemas["EstimationResponse"]["required"]),
            {"estimation", "model", "provider"},
        )
        self.generate_estimation.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
