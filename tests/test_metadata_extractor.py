"""Pruebas del extractor heurístico de la memoria (project_metadata).

Cada test da un texto de ejemplo, como lo escribiría un cliente o el modelo,
y comprueba qué hechos saca el extractor. No hay ningún modelo de IA.
"""

import unittest

from app.services.metadata_extractor import (
    facts_from_reply,
    facts_from_transcript,
    merge_metadata,
    update_metadata_from_turn,
)
from app.services.sessions import ProjectMetadata

MODEL_REPLY = """## 1. Resumen
Portal web para reservar salas...

<hechos_del_proyecto>
Proyecto: Portal de reservas
Equipo asumido: 3 personas
Tecnologías: React, FastAPI y PostgreSQL
Alcance acordado: sin definir
</hechos_del_proyecto>"""


class FactsFromModelReplyTests(unittest.TestCase):
    def test_reads_the_facts_block_at_the_end_of_the_reply(self):
        facts = facts_from_reply(MODEL_REPLY)

        self.assertEqual(facts.project_name, "Portal de reservas")
        self.assertEqual(facts.assumed_team_size, 3)
        self.assertEqual(facts.mentioned_technologies, ["React", "FastAPI", "PostgreSQL"])
        self.assertIsNone(facts.agreed_scope)  # "sin definir" = todavía no se sabe

    def test_without_block_nothing_is_extracted(self):
        """El modelo nombra Redis como sugerencia: eso no es una decisión del proyecto."""
        facts = facts_from_reply("Podríais usar Redis para la caché. Total: 80 horas.")

        self.assertTrue(facts.is_empty())

    def test_team_size_written_with_letters(self):
        reply = "<hechos_del_proyecto>\nEquipo asumido: cuatro desarrolladores\n</hechos_del_proyecto>"

        self.assertEqual(facts_from_reply(reply).assumed_team_size, 4)

    def test_template_placeholders_copied_by_the_model_are_ignored(self):
        reply = (
            "<hechos_del_proyecto>\nProyecto: nombre del proyecto\n"
            "Equipo asumido: número de personas\n</hechos_del_proyecto>"
        )

        self.assertTrue(facts_from_reply(reply).is_empty())


class FactsFromTranscriptTests(unittest.TestCase):
    def test_typical_client_sentences(self):
        """Frases como las diría el cliente en la reunión."""
        cases = {
            "El proyecto se llama Portal de Reservas.": ("project_name", "Portal de Reservas"),
            "El proyecto se llamará «Reservas 360» y queremos web.": ("project_name", "Reservas 360"),
            "El proyecto se llama Portal y queremos que sea rápido.": ("project_name", "Portal"),
            "Suponed un equipo de tres personas.": ("assumed_team_size", 3),
            "Contamos con 4 desarrolladores.": ("assumed_team_size", 4),
            "Alcance acordado: solo la web, sin app móvil": ("agreed_scope", "solo la web, sin app móvil"),
            "Hemos acordado que la primera fase no incluye pagos.": ("agreed_scope", "la primera fase no incluye pagos"),
        }
        for sentence, (field, expected) in cases.items():
            with self.subTest(sentence):
                self.assertEqual(getattr(facts_from_transcript(sentence), field), expected)

    def test_technologies_from_an_explicit_list_and_by_name(self):
        facts = facts_from_transcript(
            "Las tecnologías usadas son React y FastAPI. Ya tenemos PostgreSQL en producción."
        )

        self.assertEqual(facts.mentioned_technologies, ["React", "FastAPI", "PostgreSQL"])

    def test_no_false_positives_in_ordinary_sentences(self):
        """"tecnologías usar" no es una tecnología, y "javascript" no es "Java"."""
        facts = facts_from_transcript("No sabemos qué tecnologías usar; quizá algo en javascript.")

        self.assertEqual(facts.mentioned_technologies, ["JavaScript"])


class MergeTests(unittest.TestCase):
    def test_new_values_replace_and_missing_ones_are_kept(self):
        current = ProjectMetadata(project_name="Portal", assumed_team_size=2, agreed_scope="Todo")

        merged = merge_metadata(current, ProjectMetadata(assumed_team_size=4))

        self.assertEqual(merged.project_name, "Portal")  # no se repite, pero no se borra
        self.assertEqual(merged.assumed_team_size, 4)  # el dato nuevo gana
        self.assertEqual(merged.agreed_scope, "Todo")

    def test_technologies_are_added_without_duplicates(self):
        current = ProjectMetadata(mentioned_technologies=["Kafka", "React"])

        merged = merge_metadata(current, ProjectMetadata(mentioned_technologies=["react", "FastAPI"]))

        self.assertEqual(merged.mentioned_technologies, ["Kafka", "React", "FastAPI"])


class WholeTurnTests(unittest.TestCase):
    def test_first_turn_fills_the_empty_memory(self):
        memory = update_metadata_from_turn(ProjectMetadata(), "Necesitamos un portal.", MODEL_REPLY)

        self.assertEqual(memory.project_name, "Portal de reservas")
        self.assertEqual(memory.assumed_team_size, 3)

    def test_what_the_client_says_explicitly_wins_over_the_model_block(self):
        """El cliente cambia a 4 personas; aunque el bloque del modelo diga 3, gana el 4."""
        memory = update_metadata_from_turn(
            ProjectMetadata(), "Ahora con un equipo de 4 personas.", MODEL_REPLY
        )

        self.assertEqual(memory.assumed_team_size, 4)
        self.assertEqual(memory.project_name, "Portal de reservas")  # el resto viene del bloque


if __name__ == "__main__":
    unittest.main()
