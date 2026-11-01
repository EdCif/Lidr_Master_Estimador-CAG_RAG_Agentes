"""El chunker estructural: corta cada presupuesto en trozos, uno por componente.

Un *chunk* es el trozo de texto que luego se convierte en un vector. Aquí no
hay que adivinar dónde cortar: el presupuesto ya viene dividido en
componentes (un login, un inventario, un hosting...), así que seguimos esa
estructura. Por eso se llama «estructural».

Las tres decisiones de diseño (lección 0704 del máster):

1. **Granularidad: un componente = un chunk.** Es la unidad que razona el
   negocio («¿cuánto costó un login OAuth?»). Con el presupuesto entero, el
   vector sería una mezcla borrosa de temas; con campos sueltos, se perdería
   el sentido.
2. **Contenido: texto legible con el contexto del presupuesto delante.** Cada
   chunk empieza con una cabecera (proyecto, sector, año, tecnología
   principal). Sin ella, «Backend de autenticación» no diría si es de un
   banco o de una tienda online.
3. **Metadata: lo que sirve para filtrar va aparte.** Año, horas, complejidad
   o sector viajan con el chunk pero NO se vectorizan: los embeddings no
   entienden bien números ni fechas, así que «presupuestos de 2024» se filtra,
   no se le pide al vector.

Un chunk resultante (el texto, tal cual se vectorizará)::

    [Proyecto: API de banca móvil con autenticación OAuth 2.0 y cumplimiento de PSD2]
    [Sector del cliente: finanzas | Año: 2024 | Tecnología principal: ruby_on_rails]

    Componente: Backend de autenticación OAuth 2.0
    Descripción: Implementación de los flujos OAuth 2.0 (código de autorización...
    Tecnologías: ruby_on_rails, postgresql, redis
    Complejidad: alta
    Horas estimadas: 120

    chunk_id:    BUD-2024-001::AUTH-001
    metadata:    {budget_id, component_id, client_sector: "finance", year: 2024, ...}
    token_count: unos 130

Qué NO hace (se ve en el directo): no solapa trozos ni parte descripciones
largas. Si un componente es enorme se avisa (más de 1.000 tokens) o se
rechaza (más de 8.191, el máximo que admite el modelo de embeddings).
"""

from collections.abc import Callable
from functools import lru_cache

from app.embedding_pipeline.schemas import Budget, BudgetComponent, Chunk
from app.services.json_logger import make_json_logger

# Nombre del logger: así se buscan sus líneas en la consola y en los tests.
LOGGER_NAME = "embedding_chunker"
_log = make_json_logger(LOGGER_NAME)

# Modelo con el que se cuentan los tokens. Todos los modelos de embeddings de
# OpenAI actuales comparten el mismo vocabulario, así que sirve aunque
# EMBEDDING_MODEL cambie a otro de la misma familia.
TOKENIZER_MODEL = "text-embedding-3-small"

# A partir de cuántos tokens un chunk se considera «sospechosamente grande»:
# se procesa igual, pero se deja un aviso en el log para poder revisarlo.
WARN_TOKENS = 1_000

# Máximo de tokens que admite OpenAI por texto en los modelos de embeddings.
# Por encima, la API falla; preferimos fallar antes y con un mensaje claro.
MAX_TOKENS = 8_191

# Cómo se cuentan los tokens de un texto. Es una función que se «inyecta» (se
# pasa desde fuera) para que los tests usen una falsa y no necesiten tiktoken.
TokenCounter = Callable[[str], int]

# Lo que se embebe va en español (el texto es para el significado), pero en la
# metadata el sector y la complejidad siguen en inglés (son para filtrar).
SECTOR_LABELS = {
    "finance": "finanzas",
    "ecommerce": "comercio electrónico",
    "healthcare": "salud",
    "industrial": "industria",
}
COMPLEXITY_LABELS = {"low": "baja", "medium": "media", "high": "alta"}

# Plantillas del texto de cada chunk. Si se prefieren las etiquetas del
# enunciado en inglés («Project», «Client sector»...), se cambian aquí.
HEADER_TEMPLATE = (
    "[Proyecto: {project_summary}]\n"
    "[Sector del cliente: {sector} | Año: {year} | Tecnología principal: {main_technology}]"
)
BODY_TEMPLATE = (
    "Componente: {name}\n"
    "Descripción: {description}\n"
    "Tecnologías: {tech_stack}\n"
    "Complejidad: {complexity}\n"
    "Horas estimadas: {hours}"
)


class ChunkTooLargeError(ValueError):
    """Un chunk supera los tokens que admite el modelo de embeddings.

    Lleva el ``chunk_id`` y el tamaño para que el endpoint pueda decirle al
    cliente exactamente qué componente hay que acortar.
    """

    def __init__(self, chunk_id: str, token_count: int, limit: int = MAX_TOKENS) -> None:
        self.chunk_id = chunk_id
        self.token_count = token_count
        self.limit = limit
        super().__init__(
            f"El chunk {chunk_id} tiene {token_count} tokens y el modelo admite como "
            f"máximo {limit}. Acorta la descripción de ese componente."
        )


@lru_cache(maxsize=1)
def _encoding():
    """Carga el contador de tokens de OpenAI, solo la primera vez que hace falta.

    tiktoken descarga su vocabulario la primera vez (y lo guarda en el
    ordenador), así que no lo cargamos al importar el módulo: importar este
    fichero no debe necesitar internet.
    """
    import tiktoken

    return tiktoken.encoding_for_model(TOKENIZER_MODEL)


def count_tokens_with_tiktoken(text: str) -> int:
    """Cuenta los tokens de un texto igual que lo hace OpenAI (y por tanto, lo que cobra).

    ``disallowed_special=()`` hace que un texto que contenga algo como
    ``<|endoftext|>`` se cuente como texto normal, en vez de dar error. Un
    presupuesto no tiene por qué saber que esa cadena es especial para tiktoken.
    """
    return len(_encoding().encode(text, disallowed_special=()))


class JSONStructuralChunker:
    """Trocea presupuestos con un único método público: ``chunk()``.

    ``count_tokens`` es opcional: por defecto se usa tiktoken; los tests pasan
    una función falsa para no depender de él.
    """

    def __init__(self, count_tokens: TokenCounter | None = None) -> None:
        self._count_tokens = count_tokens or count_tokens_with_tiktoken

    def chunk(self, budgets: list[Budget]) -> list[Chunk]:
        """Devuelve un chunk por cada componente de cada presupuesto, en orden.

        Para cada presupuesto: se prepara su cabecera una vez, y por cada
        componente se monta el texto, la metadata, el id y los tokens. Al
        acabar un presupuesto se deja una línea en el log con cuántos chunks
        produjo.

        Lanza ``ChunkTooLargeError`` si algún chunk pasa de ``MAX_TOKENS``.
        """
        chunks: list[Chunk] = []
        for budget in budgets:
            header = self._parent_header(budget)
            budget_tokens = 0

            for component in budget.components:
                chunk_id = f"{budget.budget_id}::{component.component_id}"
                text = self._render_text(header, component)
                token_count = self._count_tokens(text)

                if token_count > MAX_TOKENS:
                    raise ChunkTooLargeError(chunk_id, token_count)
                if token_count > WARN_TOKENS:
                    _log.warning(
                        "chunk_larger_than_expected",
                        chunk_id=chunk_id,
                        tokens=token_count,
                        warn_above=WARN_TOKENS,
                    )

                chunks.append(
                    Chunk(
                        chunk_id=chunk_id,
                        text=text,
                        metadata=self._build_metadata(budget, component),
                        token_count=token_count,
                    )
                )
                budget_tokens += token_count

            _log.info(
                "chunker_budget_done",
                budget_id=budget.budget_id,
                chunks=len(budget.components),
                tokens=budget_tokens,
            )
        return chunks

    def _parent_header(self, budget: Budget) -> str:
        """La cabecera de contexto: dos líneas que sitúan el chunk en su presupuesto.

        Es la mejora más barata y rentable que se conoce (lección 0703): sin
        ella, todos los «backend de autenticación» se parecen entre sí.
        """
        sector = budget.client_metadata.sector
        return HEADER_TEMPLATE.format(
            project_summary=budget.project_summary,
            sector=SECTOR_LABELS.get(sector, sector),
            year=budget.year,
            main_technology=budget.main_technology,
        )

    def _render_text(self, header: str, component: BudgetComponent) -> str:
        """El texto que se vectoriza: la cabecera del presupuesto + el detalle del componente."""
        body = BODY_TEMPLATE.format(
            name=component.name,
            description=component.description,
            tech_stack=", ".join(component.tech_stack),
            complexity=COMPLEXITY_LABELS.get(component.complexity, component.complexity),
            # «:g» escribe 120.0 como 120 y 7.5 como 7.5.
            hours=f"{component.estimated_hours:g}",
        )
        return f"{header}\n\n{body}"

    def _build_metadata(self, budget: Budget, component: BudgetComponent) -> dict:
        """Los datos para filtrar: viajan con el chunk pero NO se vectorizan.

        Aquí el sector y la complejidad van en inglés (``finance``, ``high``):
        son valores cerrados para filtrar, no texto para leer. Algunos (sector,
        año, tecnología) están también dentro del texto: en el texto aportan
        significado a la búsqueda; en la metadata permiten filtrar con
        exactitud.
        """
        return {
            # La sesión 8 guarda esta columna en su tabla de chunks.
            "chunk_type": "budget_component",
            "budget_id": budget.budget_id,
            "component_id": component.component_id,
            "client_sector": budget.client_metadata.sector,
            "main_technology": budget.main_technology,
            "year": budget.year,
            "complexity": component.complexity,
            "estimated_hours": component.estimated_hours,
            "tech_stack": list(component.tech_stack),
        }
