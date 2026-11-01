"""Pruebas del chunker estructural de presupuestos (sesión 7).

El chunker recibe presupuestos y devuelve un chunk por componente. Aquí se
comprueba qué texto, qué metadata, qué id y cuántos tokens sale en cada caso.
No usan tiktoken ni red: el contador de tokens es una función falsa que se
pasa al chunker (así los tests no descargan nada).
"""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.embedding_pipeline import chunker as chunker_module
from app.embedding_pipeline.chunker import (
    LOGGER_NAME,
    MAX_TOKENS,
    WARN_TOKENS,
    ChunkTooLargeError,
    JSONStructuralChunker,
    count_tokens_with_tiktoken,
)
from app.embedding_pipeline.schemas import Budget, IngestRequest

DATASET = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"

BUDGET_DATA = {
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
            "tech_stack": ["ruby_on_rails", "postgresql", "redis"],
            "estimated_hours": 120,
            "complexity": "high",
            "dependencies": [],
        },
        {
            "component_id": "NOTIF-002",
            "name": "Notificaciones",
            "description": "Avisos por push y correo.",
            "tech_stack": ["ruby_on_rails", "redis"],
            "estimated_hours": 60,
            "complexity": "low",
            "dependencies": ["AUTH-001"],
        },
    ],
}

# Lo que tiene que salir, escrito a mano, para el primer componente.
EXPECTED_TEXT = (
    "[Proyecto: API de banca móvil]\n"
    "[Sector del cliente: finanzas | Año: 2024 | Tecnología principal: ruby_on_rails]\n"
    "\n"
    "Componente: Backend de autenticación\n"
    "Descripción: Flujos OAuth 2.0 con JWT.\n"
    "Tecnologías: ruby_on_rails, postgresql, redis\n"
    "Complejidad: alta\n"
    "Horas estimadas: 120"
)


def make_budget(**changes) -> Budget:
    data = copy.deepcopy(BUDGET_DATA)
    data.update(changes)
    return Budget.model_validate(data)


def words_counter(text: str) -> int:
    """Contador falso: una palabra = un token. Basta para los tests."""
    return len(text.split())


def fixed_counter(tokens: int):
    """Contador falso que dice siempre lo mismo, para probar los límites."""
    return lambda text: tokens


class ChunkContentTests(unittest.TestCase):
    def setUp(self):
        self.chunker = JSONStructuralChunker(count_tokens=words_counter)

    def test_one_chunk_per_component_in_order(self):
        second = copy.deepcopy(BUDGET_DATA)
        second["budget_id"] = "BUD-2024-902"

        chunks = self.chunker.chunk([make_budget(), Budget.model_validate(second)])

        self.assertEqual(
            [chunk.chunk_id for chunk in chunks],
            [
                "BUD-2024-901::AUTH-001",
                "BUD-2024-901::NOTIF-002",
                "BUD-2024-902::AUTH-001",
                "BUD-2024-902::NOTIF-002",
            ],
        )

    def test_no_budgets_give_no_chunks(self):
        self.assertEqual(self.chunker.chunk([]), [])

    def test_the_text_is_exactly_the_expected_one(self):
        chunks = self.chunker.chunk([make_budget()])

        self.assertEqual(chunks[0].text, EXPECTED_TEXT)

    def test_every_chunk_of_a_budget_starts_with_the_same_header(self):
        chunks = self.chunker.chunk([make_budget()])

        header = EXPECTED_TEXT.split("\n\n")[0]
        for chunk in chunks:
            self.assertTrue(chunk.text.startswith(header), chunk.chunk_id)
        # Y lo que cambia es el detalle del componente.
        self.assertIn("Componente: Notificaciones", chunks[1].text)
        self.assertNotIn("Notificaciones", chunks[0].text)

    def test_hours_are_written_without_a_useless_decimal(self):
        data = copy.deepcopy(BUDGET_DATA)
        data["components"][0]["estimated_hours"] = 7.5
        data["components"][1]["estimated_hours"] = 60.0

        chunks = self.chunker.chunk([Budget.model_validate(data)])

        self.assertIn("Horas estimadas: 7.5", chunks[0].text)
        self.assertIn("Horas estimadas: 60", chunks[1].text)
        self.assertNotIn("60.0", chunks[1].text)

    def test_sector_and_complexity_are_translated_in_the_text(self):
        data = copy.deepcopy(BUDGET_DATA)
        data["client_metadata"]["sector"] = "healthcare"

        chunks = self.chunker.chunk([Budget.model_validate(data)])

        self.assertIn("Sector del cliente: salud", chunks[0].text)
        self.assertIn("Complejidad: baja", chunks[1].text)

    def test_data_that_does_not_change_the_meaning_stays_out_of_the_text(self):
        """Cliente, país, ids y dependencias son para trazar o filtrar, no para el significado."""
        chunks = self.chunker.chunk([make_budget()])

        for chunk in chunks:
            for hidden in ("Cliente de ejemplo", "BUD-2024-901", "AUTH-001", "NOTIF-002", "ES"):
                self.assertNotIn(hidden, chunk.text.split(), f"{hidden} en {chunk.chunk_id}")
        self.assertNotIn("Cliente de ejemplo", chunks[0].text)
        self.assertNotIn("BUD-2024-901", chunks[0].text)


class ChunkMetadataTests(unittest.TestCase):
    def setUp(self):
        self.chunker = JSONStructuralChunker(count_tokens=words_counter)

    def test_metadata_has_exactly_the_filter_fields(self):
        chunks = self.chunker.chunk([make_budget()])

        self.assertEqual(
            chunks[0].metadata,
            {
                "chunk_type": "budget_component",
                "budget_id": "BUD-2024-901",
                "component_id": "AUTH-001",
                "client_sector": "finance",
                "main_technology": "ruby_on_rails",
                "year": 2024,
                "complexity": "high",
                "estimated_hours": 120,
                "tech_stack": ["ruby_on_rails", "postgresql", "redis"],
            },
        )

    def test_metadata_keeps_the_english_filter_values_not_the_translated_ones(self):
        metadata = self.chunker.chunk([make_budget()])[0].metadata

        self.assertEqual(metadata["client_sector"], "finance")  # no «finanzas»
        self.assertEqual(metadata["complexity"], "high")  # no «alta»

    def test_the_stack_in_the_metadata_is_a_copy(self):
        budget = make_budget()

        metadata = self.chunker.chunk([budget])[0].metadata
        metadata["tech_stack"].append("otra_cosa")

        self.assertEqual(budget.components[0].tech_stack, ["ruby_on_rails", "postgresql", "redis"])


class TokenCountTests(unittest.TestCase):
    def test_token_count_comes_from_the_injected_counter_over_the_final_text(self):
        seen: list[str] = []

        def spy(text: str) -> int:
            seen.append(text)
            return 77

        chunks = JSONStructuralChunker(count_tokens=spy).chunk([make_budget()])

        self.assertEqual([chunk.token_count for chunk in chunks], [77, 77])
        self.assertEqual(seen, [chunk.text for chunk in chunks])  # se cuenta el texto ya montado

    def test_a_big_chunk_is_processed_but_leaves_a_warning(self):
        chunker = JSONStructuralChunker(count_tokens=fixed_counter(WARN_TOKENS + 1))

        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            chunks = chunker.chunk([make_budget()])

        self.assertEqual(len(chunks), 2)
        warning = json.loads(logs.records[0].getMessage())
        self.assertEqual(warning["event"], "chunk_larger_than_expected")
        self.assertEqual(warning["chunk_id"], "BUD-2024-901::AUTH-001")
        self.assertEqual(warning["tokens"], WARN_TOKENS + 1)

    def test_exactly_the_warning_limit_is_not_warned(self):
        chunker = JSONStructuralChunker(count_tokens=fixed_counter(WARN_TOKENS))

        with self.assertNoLogs(LOGGER_NAME, level="WARNING"):
            chunker.chunk([make_budget()])

    def test_exactly_the_model_limit_is_accepted(self):
        chunker = JSONStructuralChunker(count_tokens=fixed_counter(MAX_TOKENS))

        with self.assertLogs(LOGGER_NAME, level="WARNING"):  # avisa, pero no falla
            chunks = chunker.chunk([make_budget()])

        self.assertEqual(len(chunks), 2)

    def test_above_the_model_limit_is_a_clear_error(self):
        chunker = JSONStructuralChunker(count_tokens=fixed_counter(MAX_TOKENS + 1))

        with self.assertRaises(ChunkTooLargeError) as caught:
            chunker.chunk([make_budget()])

        error = caught.exception
        self.assertEqual(error.chunk_id, "BUD-2024-901::AUTH-001")
        self.assertEqual(error.token_count, MAX_TOKENS + 1)
        self.assertIn("BUD-2024-901::AUTH-001", str(error))
        self.assertIsInstance(error, ValueError)

    def test_the_limits_are_the_ones_of_the_assignment(self):
        self.assertEqual(WARN_TOKENS, 1_000)
        self.assertEqual(MAX_TOKENS, 8_191)


class DefaultTokenCounterTests(unittest.TestCase):
    """El contador por defecto usa tiktoken; aquí se sustituye por uno falso (sin red)."""

    def test_counts_with_the_tiktoken_encoding_and_treats_special_text_as_normal(self):
        fake_encoding = MagicMock()
        fake_encoding.encode.return_value = [1, 2, 3, 4]

        with patch.object(chunker_module, "_encoding", return_value=fake_encoding):
            tokens = count_tokens_with_tiktoken("texto con <|endoftext|> dentro")

        self.assertEqual(tokens, 4)
        fake_encoding.encode.assert_called_once_with("texto con <|endoftext|> dentro", disallowed_special=())

    def test_importing_the_module_does_not_load_tiktoken(self):
        """El vocabulario se descarga la primera vez que se usa, no al importar."""
        self.assertEqual(chunker_module._encoding.cache_info().currsize, 0)


class ChunkerLogTests(unittest.TestCase):
    def test_one_line_per_budget_with_its_chunks_and_tokens(self):
        second = copy.deepcopy(BUDGET_DATA)
        second["budget_id"] = "BUD-2024-902"
        second["components"] = second["components"][:1]
        chunker = JSONStructuralChunker(count_tokens=fixed_counter(10))

        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            chunker.chunk([make_budget(), Budget.model_validate(second)])

        lines = [json.loads(record.getMessage()) for record in logs.records]
        self.assertEqual(
            [(line["event"], line["budget_id"], line["chunks"], line["tokens"]) for line in lines],
            [("chunker_budget_done", "BUD-2024-901", 2, 20), ("chunker_budget_done", "BUD-2024-902", 1, 10)],
        )


class SampleDatasetChunkingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = json.loads(DATASET.read_text(encoding="utf-8"))
        cls.budgets = IngestRequest.model_validate({"budgets": raw}).budgets
        cls.chunks = JSONStructuralChunker(count_tokens=words_counter).chunk(cls.budgets)

    def test_one_chunk_per_component_of_the_whole_dataset(self):
        components = sum(len(budget.components) for budget in self.budgets)

        self.assertEqual(len(self.chunks), components)
        self.assertEqual(len({chunk.chunk_id for chunk in self.chunks}), components)

    def test_every_chunk_has_its_header_and_its_component(self):
        pairs = [(b, c) for b in self.budgets for c in b.components]

        for chunk, (budget, component) in zip(self.chunks, pairs, strict=True):
            with self.subTest(chunk=chunk.chunk_id):
                self.assertTrue(chunk.text.startswith(f"[Proyecto: {budget.project_summary}]"))
                self.assertIn(f"Componente: {component.name}", chunk.text)
                self.assertEqual(chunk.metadata["budget_id"], budget.budget_id)
                self.assertEqual(chunk.metadata["component_id"], component.component_id)

    def test_no_chunk_of_the_dataset_is_too_big(self):
        """Con descripciones de una o dos frases, ningún chunk se acerca al aviso."""
        self.assertLess(max(chunk.token_count for chunk in self.chunks), 200)


if __name__ == "__main__":
    unittest.main()
