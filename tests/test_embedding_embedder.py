"""Pruebas del embedder de OpenAI (sesión 7).

Se usa un «OpenAI de mentira» (``FakeClient``) que apunta cuántas llamadas
recibe, cuántos textos lleva cada una y, si se le pide, falla a propósito
(límite de peticiones, clave inválida...). Así se comprueban los lotes, el
orden y los reintentos sin red, sin clave y sin gastar. La espera entre
reintentos también se sustituye: el test no se duerme de verdad.
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import AuthenticationError, RateLimitError
from pydantic import SecretStr

from app.embedding_pipeline import embedder as embedder_module
from app.embedding_pipeline.embedder import (
    BATCH_SIZE,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    LOGGER_NAME,
    PRICE_USD_PER_MILLION_TOKENS,
    RETRY_DELAYS_S,
    EmbeddingResponseError,
    OpenAIEmbedder,
    create_embedder,
    estimate_cost_usd,
)
from app.embedding_pipeline.schemas import Chunk


def api_error(cls, status: int):
    """Un error de la API de OpenAI como el que lanzaría el SDK de verdad."""
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    return cls("error de prueba", response=httpx.Response(status, request=request), body=None)


def rate_limit() -> RateLimitError:
    return api_error(RateLimitError, 429)


def marker_vector(marker: float, size: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Un vector cuyo primer número identifica de qué texto salió (para comprobar el orden)."""
    return [marker] + [0.0] * (size - 1)


class FakeEmbeddings:
    """Hace de ``client.embeddings``: guarda las llamadas y devuelve vectores inventados."""

    def __init__(self, script=None, size=EMBEDDING_DIMENSIONS, drop=0, reverse=False):
        self.calls: list[dict] = []
        self.script = list(script or [])  # errores que lanza antes de acertar, en orden
        self.size = size
        self.drop = drop  # cuántos vectores «se pierde» OpenAI (para probar el fallo duro)
        self.reverse = reverse  # devuelve los datos desordenados

    def create(self, *, model, input):
        self.calls.append({"model": model, "input": list(input)})
        if self.script:
            error = self.script.pop(0)
            if error is not None:
                raise error
        # El texto «texto #7» da un vector con marker 7.
        data = [
            SimpleNamespace(index=index, embedding=marker_vector(float(text.split("#")[1]), self.size))
            for index, text in enumerate(input)
        ]
        if self.drop:
            data = data[: -self.drop]
        if self.reverse:
            data = data[::-1]
        return SimpleNamespace(data=data, usage=SimpleNamespace(total_tokens=3 * len(input)))


class FakeClient:
    def __init__(self, **kwargs):
        self.embeddings = FakeEmbeddings(**kwargs)


def make_chunks(count: int) -> list[Chunk]:
    return [
        Chunk(chunk_id=f"B::C{i}", text=f"texto #{i}", metadata={"i": i}, token_count=5)
        for i in range(count)
    ]


def make_embedder(**client_kwargs):
    """Un embedder con cliente falso, sin esperas reales y con contador de tokens falso."""
    client = FakeClient(**client_kwargs)
    sleeps: list[float] = []
    embedder = OpenAIEmbedder(client, sleep=sleeps.append, count_tokens=lambda text: len(text.split()))
    return embedder, client, sleeps


class BatchingTests(unittest.TestCase):
    def test_250_chunks_go_in_three_calls_of_100_100_50(self):
        embedder, client, _ = make_embedder()

        embedded = embedder.embed_many(make_chunks(250))

        self.assertEqual([len(call["input"]) for call in client.embeddings.calls], [100, 100, 50])
        self.assertEqual(len(embedded), 250)

    def test_the_order_is_kept_and_each_vector_matches_its_chunk(self):
        embedder, _, _ = make_embedder()

        embedded = embedder.embed_many(make_chunks(250))

        for position, item in enumerate(embedded):
            self.assertEqual(item.chunk_id, f"B::C{position}")
            self.assertEqual(item.embedding[0], position)  # el vector es el de SU texto

    def test_the_chunk_data_is_kept(self):
        embedder, _, _ = make_embedder()
        chunk = Chunk(chunk_id="B::X", text="texto #3", metadata={"year": 2024, "tech_stack": ["go"]}, token_count=9)

        (item,) = embedder.embed_many([chunk])

        self.assertEqual(item.chunk_id, "B::X")
        self.assertEqual(item.text, "texto #3")
        self.assertEqual(item.metadata, {"year": 2024, "tech_stack": ["go"]})
        self.assertEqual(item.token_count, 9)
        self.assertEqual(len(item.embedding), EMBEDDING_DIMENSIONS)

    def test_exactly_one_batch_and_one_more_than_a_batch(self):
        for count, calls in ((BATCH_SIZE, 1), (BATCH_SIZE + 1, 2)):
            with self.subTest(count=count):
                embedder, client, _ = make_embedder()
                embedder.embed_many(make_chunks(count))
                self.assertEqual(len(client.embeddings.calls), calls)

    def test_no_chunks_means_no_call(self):
        embedder, client, _ = make_embedder()

        self.assertEqual(embedder.embed_many([]), [])
        self.assertEqual(client.embeddings.calls, [])

    def test_the_configured_model_is_the_one_sent(self):
        client = FakeClient()
        embedder = OpenAIEmbedder(client, model="otro-modelo", sleep=lambda s: None)

        embedder.embed_many(make_chunks(1))

        self.assertEqual(client.embeddings.calls[0]["model"], "otro-modelo")

    def test_the_default_model_and_sizes_are_the_ones_of_the_assignment(self):
        self.assertEqual(EMBEDDING_MODEL, "text-embedding-3-small")
        self.assertEqual(EMBEDDING_DIMENSIONS, 1536)
        self.assertEqual(BATCH_SIZE, 100)
        self.assertEqual(RETRY_DELAYS_S, (1, 2, 4))

    def test_data_arriving_out_of_order_is_put_back_by_index(self):
        embedder, _, _ = make_embedder(reverse=True)

        embedded = embedder.embed_many(make_chunks(5))

        self.assertEqual([item.embedding[0] for item in embedded], [0, 1, 2, 3, 4])


class EmbedOneTests(unittest.TestCase):
    def test_returns_the_vector_of_that_text(self):
        embedder, client, _ = make_embedder()

        vector = embedder.embed_one("texto #7")

        self.assertEqual(len(vector), EMBEDDING_DIMENSIONS)
        self.assertEqual(vector[0], 7)
        self.assertEqual(client.embeddings.calls[0]["input"], ["texto #7"])


class RetryTests(unittest.TestCase):
    def test_a_rate_limit_followed_by_success_recovers(self):
        embedder, client, sleeps = make_embedder(script=[rate_limit()])

        embedded = embedder.embed_many(make_chunks(2))

        self.assertEqual(len(embedded), 2)
        self.assertEqual(len(client.embeddings.calls), 2)  # el fallo y el acierto
        self.assertEqual(sleeps, [1])

    def test_three_rate_limits_in_a_row_still_recover_waiting_1_2_4(self):
        embedder, client, sleeps = make_embedder(script=[rate_limit(), rate_limit(), rate_limit()])

        embedder.embed_many(make_chunks(1))

        self.assertEqual(sleeps, [1, 2, 4])
        self.assertEqual(len(client.embeddings.calls), 4)

    def test_four_rate_limits_in_a_row_raise_the_error(self):
        embedder, client, sleeps = make_embedder(script=[rate_limit()] * 4)

        with self.assertRaises(RateLimitError):
            embedder.embed_many(make_chunks(1))

        self.assertEqual(sleeps, [1, 2, 4])
        self.assertEqual(len(client.embeddings.calls), 4)

    def test_any_other_error_is_not_retried(self):
        """Una clave mala no se arregla esperando: sube a la primera, sin reintentos."""
        embedder, client, sleeps = make_embedder(script=[api_error(AuthenticationError, 401)])

        with self.assertRaises(AuthenticationError):
            embedder.embed_many(make_chunks(1))

        self.assertEqual(len(client.embeddings.calls), 1)
        self.assertEqual(sleeps, [])

    def test_the_retry_count_is_per_batch(self):
        """Cada lote tiene sus propios 3 reintentos."""
        embedder, client, sleeps = make_embedder(script=[rate_limit(), None, rate_limit()])

        embedder.embed_many(make_chunks(150))  # lote 1 (100) y lote 2 (50)

        self.assertEqual(sleeps, [1, 1])
        self.assertEqual(len(client.embeddings.calls), 4)


class HardFailureTests(unittest.TestCase):
    def test_fewer_vectors_than_texts_is_an_error(self):
        embedder, _, _ = make_embedder(drop=1)

        with self.assertRaises(EmbeddingResponseError) as caught:
            embedder.embed_many(make_chunks(3))

        self.assertIn("3", str(caught.exception))
        self.assertIn("2", str(caught.exception))

    def test_a_vector_with_the_wrong_length_is_an_error(self):
        embedder, _, _ = make_embedder(size=10)

        with self.assertRaises(EmbeddingResponseError):
            embedder.embed_many(make_chunks(1))

    def test_embed_one_also_checks_the_length(self):
        embedder, _, _ = make_embedder(size=10)

        with self.assertRaises(EmbeddingResponseError):
            embedder.embed_one("texto #1")


class BatchLogTests(unittest.TestCase):
    def test_one_json_line_per_batch(self):
        embedder, _, _ = make_embedder(script=[rate_limit()])

        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            embedder.embed_many(make_chunks(150))

        lines = [json.loads(record.getMessage()) for record in logs.records]
        self.assertEqual(len(lines), 2)
        first, second = lines
        self.assertEqual(first["event"], "embedding_batch")
        self.assertEqual((first["batch"], first["chunks"], first["retries"]), (1, 100, 1))
        self.assertEqual((second["batch"], second["chunks"], second["retries"]), (2, 50, 0))
        # tokens = los de tiktoken (5 por chunk); api_tokens = los que «factura» OpenAI (3 por texto).
        self.assertEqual((first["tokens"], first["api_tokens"]), (500, 300))
        self.assertEqual((second["tokens"], second["api_tokens"]), (250, 150))
        self.assertEqual(first["model"], EMBEDDING_MODEL)
        self.assertGreaterEqual(first["latency_ms"], 0)

    def test_embed_one_also_leaves_a_line(self):
        embedder, _, _ = make_embedder()

        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            embedder.embed_one("texto #1")

        line = json.loads(logs.records[0].getMessage())
        self.assertEqual((line["chunks"], line["tokens"], line["api_tokens"]), (1, 2, 3))


class CostTests(unittest.TestCase):
    def test_a_million_tokens_cost_two_cents(self):
        self.assertEqual(PRICE_USD_PER_MILLION_TOKENS, 0.02)
        self.assertAlmostEqual(estimate_cost_usd(1_000_000), 0.02)

    def test_zero_tokens_cost_nothing_and_the_cost_scales(self):
        self.assertEqual(estimate_cost_usd(0), 0)
        self.assertAlmostEqual(estimate_cost_usd(5_841), 0.00011682)


class CreateEmbedderTests(unittest.TestCase):
    def test_builds_the_client_from_the_settings_without_sdk_retries(self):
        settings = SimpleNamespace(openai_api_key=SecretStr("sk-de-prueba"), embedding_model="modelo-del-env")

        with (
            patch.object(embedder_module, "get_settings", return_value=settings),
            patch.object(embedder_module, "OpenAI") as client_class,
        ):
            embedder = create_embedder()

        client_class.assert_called_once_with(api_key="sk-de-prueba", timeout=60.0, max_retries=0)
        self.assertIsInstance(embedder, OpenAIEmbedder)
        self.assertEqual(embedder._model, "modelo-del-env")
        self.assertIs(embedder._client, client_class.return_value)


if __name__ == "__main__":
    unittest.main()
