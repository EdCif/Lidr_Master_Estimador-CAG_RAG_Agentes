"""Pruebas de los moldes de datos del pipeline de embeddings (sesión 7).

Dos ideas: (1) un presupuesto bien formado pasa y uno mal formado se rechaza
con un error claro, y (2) el dataset de ejemplo que sube el repo es válido de
principio a fin. No hay red ni claves.
"""

import copy
import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.embedding_pipeline.schemas import (
    MAX_BUDGETS_PER_REQUEST,
    MAX_COMPONENTS_PER_BUDGET,
    MAX_DESCRIPTION_CHARS,
    MAX_SUMMARY_CHARS,
    Budget,
    Chunk,
    EmbeddedChunk,
    IngestRequest,
    IngestResponse,
    IngestStats,
)

DATASET = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"

# El ejemplo del enunciado, tal cual (en inglés): nuestros moldes también
# tienen que aceptar el fichero del profesor, no solo el nuestro en español.
ENUNCIADO_EXAMPLE = {
    "budget_id": "BUD-2024-014",
    "client_metadata": {"name": "FintechCorp", "sector": "finance", "country": "ES"},
    "project_summary": "Mobile banking API with OAuth 2.0 authentication and PSD2 compliance",
    "main_technology": "ruby_on_rails",
    "year": 2024,
    "total_estimated_hours": 480,
    "components": [
        {
            "component_id": "AUTH-001",
            "name": "OAuth 2.0 authentication backend",
            "description": "Implementation of OAuth 2.0 flows (authorization code, refresh token).",
            "tech_stack": ["ruby_on_rails", "postgresql", "redis"],
            "estimated_hours": 120,
            "complexity": "high",
            "dependencies": [],
        }
    ],
}


def example(**changes) -> dict:
    """Copia del ejemplo del enunciado con los cambios que pida cada test."""
    data = copy.deepcopy(ENUNCIADO_EXAMPLE)
    data.update(changes)
    return data


def two_components() -> list[dict]:
    """Dos componentes, el segundo depende del primero."""
    first = copy.deepcopy(ENUNCIADO_EXAMPLE["components"][0])
    second = copy.deepcopy(first)
    second.update({"component_id": "PSD2-002", "name": "Conectores PSD2", "dependencies": ["AUTH-001"]})
    return [first, second]


class BudgetValidationTests(unittest.TestCase):
    def test_the_example_from_the_assignment_is_valid(self):
        budget = Budget.model_validate(ENUNCIADO_EXAMPLE)

        self.assertEqual(budget.budget_id, "BUD-2024-014")
        self.assertEqual(budget.client_metadata.sector, "finance")
        self.assertEqual(budget.components[0].tech_stack, ["ruby_on_rails", "postgresql", "redis"])

    def test_dependencies_can_be_omitted(self):
        component = copy.deepcopy(ENUNCIADO_EXAMPLE["components"][0])
        del component["dependencies"]

        budget = Budget.model_validate(example(components=[component]))

        self.assertEqual(budget.components[0].dependencies, [])

    def test_unknown_sector_is_rejected(self):
        data = example()
        data["client_metadata"]["sector"] = "finanzas"

        with self.assertRaises(ValidationError):
            Budget.model_validate(data)

    def test_unknown_complexity_is_rejected(self):
        data = example()
        data["components"][0]["complexity"] = "muy alta"

        with self.assertRaises(ValidationError):
            Budget.model_validate(data)

    def test_negative_hours_are_rejected(self):
        data = example()
        data["components"][0]["estimated_hours"] = -5

        with self.assertRaises(ValidationError):
            Budget.model_validate(data)

    def test_a_budget_needs_at_least_one_component(self):
        with self.assertRaises(ValidationError):
            Budget.model_validate(example(components=[]))

    def test_year_must_be_realistic(self):
        for year in (1999, 2101):
            with self.subTest(year=year), self.assertRaises(ValidationError):
                Budget.model_validate(example(year=year))

    def test_country_is_a_two_letter_code(self):
        for country in ("España", "es", "E"):
            data = example()
            data["client_metadata"]["country"] = country
            with self.subTest(country=country), self.assertRaises(ValidationError):
                Budget.model_validate(data)

    def test_a_misspelled_field_is_an_error_not_silently_lost(self):
        """«sektor» en vez de «sector»: debe dar 422, no perderse en silencio."""
        data = example()
        data["client_metadata"]["sektor"] = "finance"

        with self.assertRaises(ValidationError):
            Budget.model_validate(data)

    def test_extra_top_level_field_is_rejected(self):
        with self.assertRaises(ValidationError):
            Budget.model_validate(example(precio=1000))

    def test_ids_cannot_contain_the_chunk_separator_or_spaces(self):
        for budget_id in ("BUD::1", "BUD 1"):
            with self.subTest(budget_id=budget_id), self.assertRaises(ValidationError):
                Budget.model_validate(example(budget_id=budget_id))

    def test_whitespace_around_texts_is_stripped(self):
        budget = Budget.model_validate(example(project_summary="   Resumen con espacios   "))

        self.assertEqual(budget.project_summary, "Resumen con espacios")


class TechnologyTests(unittest.TestCase):
    def test_a_technology_with_spaces_is_rejected(self):
        data = example()
        data["components"][0]["tech_stack"] = ["ruby on rails"]

        with self.assertRaises(ValidationError):
            Budget.model_validate(data)

    def test_repeated_technologies_are_rejected_ignoring_case(self):
        data = example()
        data["components"][0]["tech_stack"] = ["redis", "Redis"]

        with self.assertRaises(ValidationError):
            Budget.model_validate(data)

    def test_empty_stack_is_rejected(self):
        data = example()
        data["components"][0]["tech_stack"] = []

        with self.assertRaises(ValidationError):
            Budget.model_validate(data)

    def test_main_technology_cannot_have_spaces(self):
        with self.assertRaises(ValidationError):
            Budget.model_validate(example(main_technology="ruby on rails"))


class ComponentConsistencyTests(unittest.TestCase):
    def test_dependency_on_an_existing_component_is_valid(self):
        budget = Budget.model_validate(example(components=two_components()))

        self.assertEqual(budget.components[1].dependencies, ["AUTH-001"])

    def test_dependency_on_a_missing_component_is_rejected(self):
        components = two_components()
        components[1]["dependencies"] = ["NO-EXISTE-999"]

        with self.assertRaises(ValidationError) as caught:
            Budget.model_validate(example(components=components))

        self.assertIn("NO-EXISTE-999", str(caught.exception))

    def test_a_component_cannot_depend_on_itself(self):
        components = two_components()
        components[1]["dependencies"] = ["PSD2-002"]

        with self.assertRaises(ValidationError):
            Budget.model_validate(example(components=components))

    def test_repeated_component_ids_are_rejected(self):
        components = two_components()
        components[1]["component_id"] = "AUTH-001"
        components[1]["dependencies"] = []

        with self.assertRaises(ValidationError) as caught:
            Budget.model_validate(example(components=components))

        self.assertIn("AUTH-001", str(caught.exception))


class SizeLimitTests(unittest.TestCase):
    """Los topes de tamaño: el endpoint gasta dinero, así que se acota lo que puede entrar."""

    def test_limits_are_the_agreed_ones(self):
        self.assertEqual(
            (MAX_BUDGETS_PER_REQUEST, MAX_COMPONENTS_PER_BUDGET, MAX_SUMMARY_CHARS, MAX_DESCRIPTION_CHARS),
            (30, 20, 1_000, 10_000),
        )

    def test_exactly_the_limit_of_budgets_is_accepted_and_one_more_is_not(self):
        def request(count: int) -> dict:
            budgets = []
            for number in range(count):
                budget = copy.deepcopy(ENUNCIADO_EXAMPLE)
                budget["budget_id"] = f"BUD-2024-{number:03d}"
                budgets.append(budget)
            return {"budgets": budgets}

        IngestRequest.model_validate(request(MAX_BUDGETS_PER_REQUEST))
        with self.assertRaises(ValidationError):
            IngestRequest.model_validate(request(MAX_BUDGETS_PER_REQUEST + 1))

    def test_exactly_the_limit_of_components_is_accepted_and_one_more_is_not(self):
        def components(count: int) -> list[dict]:
            items = []
            for number in range(count):
                component = copy.deepcopy(ENUNCIADO_EXAMPLE["components"][0])
                component["component_id"] = f"C-{number:03d}"
                items.append(component)
            return items

        Budget.model_validate(example(components=components(MAX_COMPONENTS_PER_BUDGET)))
        with self.assertRaises(ValidationError):
            Budget.model_validate(example(components=components(MAX_COMPONENTS_PER_BUDGET + 1)))

    def test_description_and_summary_have_a_maximum_length(self):
        long_description = copy.deepcopy(ENUNCIADO_EXAMPLE["components"][0])

        long_description["description"] = "x" * MAX_DESCRIPTION_CHARS
        Budget.model_validate(example(components=[long_description]))
        long_description["description"] = "x" * (MAX_DESCRIPTION_CHARS + 1)
        with self.assertRaises(ValidationError):
            Budget.model_validate(example(components=[long_description]))

        Budget.model_validate(example(project_summary="x" * MAX_SUMMARY_CHARS))
        with self.assertRaises(ValidationError):
            Budget.model_validate(example(project_summary="x" * (MAX_SUMMARY_CHARS + 1)))


class IngestRequestTests(unittest.TestCase):
    def test_needs_at_least_one_budget(self):
        with self.assertRaises(ValidationError):
            IngestRequest.model_validate({"budgets": []})

    def test_repeated_budget_ids_are_rejected(self):
        with self.assertRaises(ValidationError):
            IngestRequest.model_validate({"budgets": [example(), example()]})

    def test_the_example_shown_in_docs_is_valid(self):
        """El ejemplo que rellena «Try it out» en /docs tiene que pasar su propia validación."""
        docs_example = IngestRequest.model_json_schema()["examples"][0]

        request = IngestRequest.model_validate(docs_example)

        self.assertEqual(len(request.budgets[0].components), 2)

    def test_the_docs_example_is_also_published_as_example_for_openapi_30(self):
        """La API usa OpenAPI 3.0.3: /docs solo rellena el formulario con ``example`` (singular)."""
        schema = IngestRequest.model_json_schema()

        self.assertEqual(schema["example"], schema["examples"][0])
        IngestRequest.model_validate(schema["example"])


class OutputModelsTests(unittest.TestCase):
    def test_embedded_chunk_is_a_chunk_plus_the_vector(self):
        chunk = Chunk(
            chunk_id="BUD-2024-014::AUTH-001",
            text="Texto del chunk",
            metadata={"budget_id": "BUD-2024-014", "year": 2024, "estimated_hours": 120.0, "tech_stack": ["redis"]},
            token_count=4,
        )
        embedded = EmbeddedChunk(**chunk.model_dump(), embedding=[0.1, 0.2, 0.3])

        self.assertIsInstance(embedded, Chunk)
        self.assertEqual(embedded.embedding, [0.1, 0.2, 0.3])

    def test_a_chunk_cannot_have_a_negative_token_count(self):
        with self.assertRaises(ValidationError):
            Chunk(chunk_id="A::B", text="x", metadata={}, token_count=-1)

    def test_a_vector_cannot_be_empty(self):
        with self.assertRaises(ValidationError):
            EmbeddedChunk(chunk_id="A::B", text="x", metadata={}, token_count=1, embedding=[])

    def test_response_carries_chunks_and_stats(self):
        response = IngestResponse(
            chunks=[],
            stats=IngestStats(total_budgets=0, total_chunks=0, total_tokens=0, estimated_cost_usd=0),
        )

        self.assertEqual(response.stats.total_chunks, 0)


class SampleDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = json.loads(DATASET.read_text(encoding="utf-8"))
        cls.budgets = IngestRequest.model_validate({"budgets": raw}).budgets

    def test_there_are_fifteen_budgets_with_unique_ids(self):
        ids = [budget.budget_id for budget in self.budgets]

        self.assertEqual(len(ids), 15)
        self.assertEqual(len(set(ids)), 15)

    def test_the_four_sectors_are_covered(self):
        sectors = {budget.client_metadata.sector for budget in self.budgets}

        self.assertEqual(sectors, {"finance", "ecommerce", "healthcare", "industrial"})

    def test_total_hours_match_the_sum_of_components(self):
        for budget in self.budgets:
            with self.subTest(budget=budget.budget_id):
                total = sum(component.estimated_hours for component in budget.components)
                self.assertEqual(budget.total_estimated_hours, total)

    def test_the_id_year_matches_the_budget_year(self):
        for budget in self.budgets:
            with self.subTest(budget=budget.budget_id):
                self.assertEqual(budget.budget_id.split("-")[1], str(budget.year))

    def test_years_and_main_technologies_vary(self):
        self.assertEqual({budget.year for budget in self.budgets}, {2023, 2024, 2025})
        self.assertGreaterEqual(len({budget.main_technology for budget in self.budgets}), 6)

    def test_every_component_uses_the_main_technology_of_its_budget(self):
        for budget in self.budgets:
            for component in budget.components:
                with self.subTest(component=f"{budget.budget_id}::{component.component_id}"):
                    self.assertIn(budget.main_technology, component.tech_stack)

    def test_the_whole_dataset_gives_unique_chunk_ids(self):
        chunk_ids = [f"{b.budget_id}::{c.component_id}" for b in self.budgets for c in b.components]

        self.assertEqual(len(chunk_ids), len(set(chunk_ids)))
        self.assertGreaterEqual(len(chunk_ids), 45)  # «unos 50 componentes»


if __name__ == "__main__":
    unittest.main()
