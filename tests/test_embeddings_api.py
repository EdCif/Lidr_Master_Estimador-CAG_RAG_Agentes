"""Tests del endpoint ``POST /embeddings/ingest`` (sesión 7), con pytest y httpx.

Recorren el endpoint como lo haría un cliente (JSON de entrada, código y JSON de
salida). Lo único falso es el embedder: llamar a OpenAI cuesta dinero y
necesita red. El chunker es el de verdad, con un contador de tokens falso.

La app de prueba lleva instalados los manejadores globales de errores de
``app/errors.py`` (los de «estimación»): así se comprueba que el router captura
sus errores antes y que no acaban convertidos en un 502 que no tiene sentido aquí.
"""

import copy
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from openai import AuthenticationError, RateLimitError

from app.config import get_settings
from app.embedding_pipeline import router as router_module
from app.embedding_pipeline.chunker import JSONStructuralChunker
from app.embedding_pipeline.router import LOGGER_NAME, get_chunker, get_embedder, router
from app.embedding_pipeline.schemas import EmbeddedChunk, IngestRequest
from app.errors import install_error_handlers

BUDGET = {
    "budget_id": "BUD-2024-901",
    "client_metadata": {"name": "Cliente de ejemplo", "sector": "finance", "country": "ES"},
    "project_summary": "API de banca móvil",
    "main_technology": "ruby_on_rails",
    "year": 2024,
    "total_estimated_hours": 180,
    "components": [
        {
            "component_id": "AUTH-001",
            "name": "Backend de autenticación",
            "description": "Flujos OAuth 2.0 con JWT.",
            "tech_stack": ["ruby_on_rails", "redis"],
            "estimated_hours": 120,
            "complexity": "high",
            "dependencies": [],
        },
        {
            "component_id": "NOTIF-002",
            "name": "Notificaciones",
            "description": "Avisos por push y correo.",
            "tech_stack": ["ruby_on_rails"],
            "estimated_hours": 60,
            "complexity": "low",
            "dependencies": ["AUTH-001"],
        },
    ],
}


def api_error(cls, status: int):
    """Un error de la API de OpenAI como el que lanzaría el SDK de verdad."""
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    return cls("error de prueba", response=httpx.Response(status, request=request), body=None)


class FakeEmbedder:
    """Embedder de mentira: apunta lo que recibe y devuelve un vector de dos números."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[list] = []

    def embed_many(self, chunks):
        self.calls.append(chunks)
        if self.error:
            raise self.error
        return [EmbeddedChunk(**chunk.model_dump(), embedding=[0.5, 0.25]) for chunk in chunks]


def build_app(embedder: FakeEmbedder, tokens_per_chunk: int = 10) -> FastAPI:
    app = FastAPI()
    app.openapi_version = "3.0.3"  # como en app/main.py
    install_error_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_embedder] = lambda: embedder
    app.dependency_overrides[get_chunker] = lambda: JSONStructuralChunker(count_tokens=lambda text: tokens_per_chunk)
    return app


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
async def client(embedder):
    transport = httpx.ASGITransport(app=build_app(embedder))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


def payload(*budgets: dict) -> dict:
    return {"budgets": [copy.deepcopy(budget) for budget in (budgets or (BUDGET,))]}


# ---------------------------------------------------------------------------
# 200: todo bien
# ---------------------------------------------------------------------------

async def test_ingest_returns_the_chunks_with_vectors_and_matching_stats(client, embedder):
    response = await client.post("/embeddings/ingest", json=payload())

    assert response.status_code == 200
    body = response.json()
    assert [chunk["chunk_id"] for chunk in body["chunks"]] == ["BUD-2024-901::AUTH-001", "BUD-2024-901::NOTIF-002"]
    assert all(chunk["embedding"] == [0.5, 0.25] for chunk in body["chunks"])
    assert body["chunks"][0]["metadata"]["client_sector"] == "finance"
    assert body["stats"] == {
        "total_budgets": 1,
        "total_chunks": 2,
        "total_tokens": 20,  # 2 chunks x 10 tokens (contador falso)
        "estimated_cost_usd": pytest.approx(20 / 1_000_000 * 0.02),
    }
    assert len(embedder.calls) == 1  # una sola llamada al embedder con todos los chunks


async def test_the_whole_sample_dataset_is_accepted(client):
    from pathlib import Path

    dataset = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"
    budgets = json.loads(dataset.read_text(encoding="utf-8"))

    response = await client.post("/embeddings/ingest", json={"budgets": budgets})

    assert response.status_code == 200
    stats = response.json()["stats"]
    assert (stats["total_budgets"], stats["total_chunks"]) == (15, 51)
    assert len(response.json()["chunks"]) == 51


async def test_ingest_leaves_a_received_and_a_done_line_in_the_log(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await client.post("/embeddings/ingest", json=payload())

    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == LOGGER_NAME]
    assert [line["event"] for line in lines] == ["embeddings_ingest_received", "embeddings_ingest_done"]
    assert (lines[0]["budgets"], lines[0]["components"]) == (1, 2)
    assert (lines[1]["total_chunks"], lines[1]["total_tokens"]) == (2, 20)


# ---------------------------------------------------------------------------
# 422: la petición no es válida (lo decide Pydantic, sin llamar al embedder)
# ---------------------------------------------------------------------------

async def test_unknown_sector_is_a_422_and_nothing_is_embedded(client, embedder):
    bad = copy.deepcopy(BUDGET)
    bad["client_metadata"]["sector"] = "finanzas"

    response = await client.post("/embeddings/ingest", json=payload(bad))

    assert response.status_code == 422
    assert "sector" in json.dumps(response.json())
    assert embedder.calls == []


async def test_a_body_without_budgets_is_a_422(client):
    for body in ({"budgets": []}, {}):
        response = await client.post("/embeddings/ingest", json=body)
        assert response.status_code == 422, body


async def test_a_misspelled_field_is_a_422_not_silently_ignored(client):
    bad = copy.deepcopy(BUDGET)
    bad["anio"] = 2024

    response = await client.post("/embeddings/ingest", json=payload(bad))

    assert response.status_code == 422


async def test_a_dependency_on_a_missing_component_is_a_422(client):
    bad = copy.deepcopy(BUDGET)
    bad["components"][1]["dependencies"] = ["NO-EXISTE-999"]

    response = await client.post("/embeddings/ingest", json=payload(bad))

    assert response.status_code == 422
    assert "NO-EXISTE-999" in json.dumps(response.json())


async def test_a_component_too_big_for_the_model_is_a_422_that_names_it(embedder):
    transport = httpx.ASGITransport(app=build_app(embedder, tokens_per_chunk=9_000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as big_client:
        response = await big_client.post("/embeddings/ingest", json=payload())

    assert response.status_code == 422
    assert "BUD-2024-901::AUTH-001" in response.json()["detail"]
    assert embedder.calls == []  # se rechaza ANTES de gastar nada en OpenAI


def budgets_payload(count: int) -> dict:
    """``count`` presupuestos distintos (2 componentes, y por tanto 2 chunks, cada uno)."""
    budgets = []
    for number in range(count):
        budget = copy.deepcopy(BUDGET)
        budget["budget_id"] = f"BUD-2024-{number:03d}"
        budgets.append(budget)
    return {"budgets": budgets}


async def test_a_request_over_the_total_token_limit_is_a_422_before_calling_openai(embedder):
    # 6 presupuestos x 2 chunks x 5.000 tokens = 60.000 > 50.000.
    transport = httpx.ASGITransport(app=build_app(embedder, tokens_per_chunk=5_000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/embeddings/ingest", json=budgets_payload(6))

    assert response.status_code == 422
    assert "60000" in response.json()["detail"] and "50000" in response.json()["detail"]
    assert embedder.calls == []  # no se gastó nada en OpenAI


async def test_a_request_exactly_at_the_total_token_limit_is_accepted(embedder):
    # 5 presupuestos x 2 chunks x 5.000 tokens = 50.000: justo en el tope.
    transport = httpx.ASGITransport(app=build_app(embedder, tokens_per_chunk=5_000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/embeddings/ingest", json=budgets_payload(5))

    assert response.status_code == 200
    assert response.json()["stats"]["total_tokens"] == 50_000
    assert len(embedder.calls) == 1


async def test_too_many_budgets_is_a_422_from_the_schema(client, embedder):
    response = await client.post("/embeddings/ingest", json=budgets_payload(31))

    assert response.status_code == 422
    assert embedder.calls == []


def test_the_token_limit_is_the_agreed_one():
    assert router_module.MAX_TOKENS_PER_REQUEST == 50_000


# ---------------------------------------------------------------------------
# 503 y 500: fallos del proveedor
# ---------------------------------------------------------------------------

async def test_rate_limit_after_the_retries_is_a_503_try_later():
    embedder = FakeEmbedder(error=api_error(RateLimitError, 429))
    transport = httpx.ASGITransport(app=build_app(embedder))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/embeddings/ingest", json=payload())

    assert response.status_code == 503
    assert "más tarde" in response.json()["detail"]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("clave interna sk-SECRETA-123 y ruta C:/privado"),
        ValueError("otro detalle interno"),
        api_error(AuthenticationError, 401),
    ],
    ids=["RuntimeError", "ValueError", "AuthenticationError"],
)
async def test_any_other_failure_is_a_generic_500_without_internal_details(error, caplog):
    embedder = FakeEmbedder(error=error)
    transport = httpx.ASGITransport(app=build_app(embedder))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            response = await client.post("/embeddings/ingest", json=payload())

    # Un 500 (no el 502 de «estimación» de app/errors.py) y con un mensaje genérico.
    assert response.status_code == 500
    assert response.json() == {"detail": "No se han podido generar los embeddings."}
    assert "SECRETA" not in response.text and "privado" not in response.text

    # El detalle SÍ queda en el log del servidor.
    failed = [json.loads(r.getMessage()) for r in caplog.records if r.name == LOGGER_NAME]
    failed = [line for line in failed if line["event"] == "embeddings_ingest_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == type(error).__name__
    assert failed[0]["detail"] == str(error)


# ---------------------------------------------------------------------------
# Que el endpoint esté donde debe estar y se vea en /docs
# ---------------------------------------------------------------------------

def test_the_route_appears_in_openapi_with_its_example_and_status_codes():
    schema = build_app(FakeEmbedder()).openapi()

    operation = schema["paths"]["/embeddings/ingest"]["post"]
    assert operation["tags"] == ["embeddings"]
    assert {"200", "422", "500", "503"} <= set(operation["responses"])
    # El ejemplo que rellena el formulario de «Try it out» en /docs.
    body_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    request_schema = schema["components"]["schemas"][body_ref.rsplit("/", 1)[1]]
    IngestRequest.model_validate(request_schema["example"])


def test_main_registers_the_router(monkeypatch):
    """La aplicación real expone POST /embeddings/ingest (y /docs la lista)."""
    monkeypatch.setenv("OPENAI_API_KEY", "clave-falsa-solo-para-el-test")
    get_settings.cache_clear()
    try:
        from app import main

        paths = main.app.openapi()["paths"]
    finally:
        get_settings.cache_clear()  # que ningún otro test herede la clave falsa

    assert "post" in paths["/embeddings/ingest"]


# ---------------------------------------------------------------------------
# Las dependencias
# ---------------------------------------------------------------------------

def test_get_embedder_is_created_once_and_reused(monkeypatch):
    created = []
    monkeypatch.setattr(router_module, "create_embedder", lambda: created.append(1) or object())
    get_embedder.cache_clear()
    try:
        first, second = get_embedder(), get_embedder()
    finally:
        get_embedder.cache_clear()

    assert first is second
    assert len(created) == 1


def test_get_chunker_gives_a_chunker():
    assert isinstance(get_chunker(), JSONStructuralChunker)
