"""Los «moldes» de datos del pipeline de embeddings.

Un molde (modelo Pydantic) describe cómo tiene que ser un dato para darlo por
bueno. FastAPI los usa en la puerta del endpoint: si el JSON que llega no
encaja, responde 422 con el motivo y no se gasta ni una llamada a OpenAI.
También los usa para pintar el formulario de ``/docs``.

Hay dos grupos:

- **Lo que entra** (un presupuesto histórico). Forma del JSON:

      {
        "budget_id": "BUD-2024-001",
        "client_metadata": {"name": "...", "sector": "finance", "country": "ES"},
        "project_summary": "...",
        "main_technology": "ruby_on_rails",
        "year": 2024,
        "total_estimated_hours": 480,
        "components": [
          {"component_id": "AUTH-001", "name": "...", "description": "...",
           "tech_stack": ["ruby_on_rails", "postgresql"], "estimated_hours": 120,
           "complexity": "high", "dependencies": []}
        ]
      }

- **Lo que sale**: ``Chunk`` (el trozo de texto listo para vectorizar),
  ``EmbeddedChunk`` (el mismo chunk con su vector) y ``IngestResponse`` (todos
  los chunks más las estadísticas de la ingesta).

Los valores «cerrados» (sector, complejidad) van en inglés, como en el
enunciado: son etiquetas para filtrar, no texto para leer. Los textos libres
(resumen, nombre y descripción) pueden ir en el idioma que sea.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Listas cerradas: cualquier otro valor se rechaza. Así un «finanzas» o un
# «hgih» mal escrito no se cuela en la metadata y ensucia los filtros de la
# sesión 8.
Sector = Literal["finance", "ecommerce", "healthcare", "industrial"]
Complexity = Literal["low", "medium", "high"]

# Lo que puede valer un dato de la metadata de un chunk. Solo tipos simples,
# para poder guardarlos tal cual en una base de datos más adelante.
MetadataValue = str | int | float | list[str]

# Los ids no pueden llevar espacios ni dos puntos: el id de cada chunk es
# «{budget_id}::{component_id}», y si el id ya contuviera «::» no se podría
# separar luego sin ambigüedad.
ID_PATTERN = r"^[A-Za-z0-9._-]+$"


class _Model(BaseModel):
    """Base común: quita espacios sobrantes y prohíbe campos que no existen.

    ``extra="forbid"`` es la clave: si alguien escribe ``"sektor"`` en vez de
    ``"sector"``, recibe un 422 en vez de que el campo se pierda en silencio.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


def _check_technology(name: str) -> str:
    """Una tecnología es una sola palabra tipo ``ruby_on_rails`` (sin espacios)."""
    name = name.strip()
    if not name:
        raise ValueError("Una tecnología no puede estar vacía.")
    if any(character.isspace() for character in name):
        raise ValueError(
            f"La tecnología «{name}» lleva espacios; escríbela en snake_case "
            "(por ejemplo, ruby_on_rails)."
        )
    return name


# ─────────────────────────── Lo que entra ───────────────────────────


class ClientMetadata(_Model):
    """Quién es el cliente del presupuesto: nombre, sector y país."""

    name: str = Field(min_length=1, max_length=200, description="Nombre del cliente.")
    sector: Sector = Field(description="Sector del cliente: finance, ecommerce, healthcare o industrial.")
    country: str = Field(
        pattern=r"^[A-Z]{2}$",
        description="País en código ISO de dos letras mayúsculas (ES, MX, DE...).",
    )


class BudgetComponent(_Model):
    """Una pieza del presupuesto (un login, un inventario, un hosting...).

    Es la unidad que razona el negocio («cuánto costó un login OAuth») y, por
    eso, cada componente acabará siendo un chunk.
    """

    component_id: str = Field(
        min_length=1,
        max_length=50,
        pattern=ID_PATTERN,
        description="Identificador dentro del presupuesto, por ejemplo AUTH-001.",
    )
    name: str = Field(min_length=1, max_length=200, description="Nombre corto del componente.")
    description: str = Field(min_length=1, description="Qué incluye el componente, en texto libre.")
    tech_stack: list[str] = Field(
        min_length=1,
        description="Tecnologías del componente, en snake_case y sin repetir.",
    )
    estimated_hours: float = Field(ge=0, description="Horas estimadas de este componente.")
    complexity: Complexity = Field(description="Complejidad: low, medium o high.")
    dependencies: list[str] = Field(
        default_factory=list,
        description="component_id de los componentes del mismo presupuesto de los que depende.",
    )

    @field_validator("tech_stack")
    @classmethod
    def clean_tech_stack(cls, value: list[str]) -> list[str]:
        """Sin espacios ni vacíos y sin repetir (sin distinguir mayúsculas)."""
        cleaned = [_check_technology(item) for item in value]
        lowered = [item.lower() for item in cleaned]
        repeated = sorted({item for item in lowered if lowered.count(item) > 1})
        if repeated:
            raise ValueError(f"Tecnologías repetidas: {', '.join(repeated)}.")
        return cleaned


class Budget(_Model):
    """Un presupuesto histórico completo: el cliente, el proyecto y sus componentes."""

    budget_id: str = Field(
        min_length=1,
        max_length=50,
        pattern=ID_PATTERN,
        description="Identificador del presupuesto, por ejemplo BUD-2024-001.",
    )
    client_metadata: ClientMetadata
    project_summary: str = Field(min_length=1, description="Resumen del proyecto en una frase.")
    main_technology: str = Field(
        min_length=1,
        max_length=100,
        description="Tecnología principal del proyecto, en snake_case.",
    )
    year: int = Field(ge=2000, le=2100, description="Año en que se hizo el proyecto.")
    total_estimated_hours: float = Field(ge=0, description="Horas totales del presupuesto.")
    components: list[BudgetComponent] = Field(
        min_length=1,
        description="Componentes del presupuesto; al menos uno.",
    )

    @field_validator("main_technology")
    @classmethod
    def check_main_technology(cls, value: str) -> str:
        return _check_technology(value)

    @model_validator(mode="after")
    def check_components_are_consistent(self) -> "Budget":
        """Los id no se repiten y cada dependencia apunta a un componente que existe."""
        ids = [component.component_id for component in self.components]
        repeated = sorted({item for item in ids if ids.count(item) > 1})
        if repeated:
            raise ValueError(
                f"Presupuesto {self.budget_id}: component_id repetidos: {', '.join(repeated)}."
            )

        known = set(ids)
        for component in self.components:
            for dependency in component.dependencies:
                if dependency == component.component_id:
                    raise ValueError(
                        f"Presupuesto {self.budget_id}: {dependency} no puede depender de sí mismo."
                    )
                if dependency not in known:
                    raise ValueError(
                        f"Presupuesto {self.budget_id}: {component.component_id} depende de "
                        f"{dependency}, que no existe en este presupuesto."
                    )
        return self


# ─────────────────────────── Lo que sale ───────────────────────────


class Chunk(_Model):
    """Un trozo de texto listo para convertirse en vector (todavía sin vector).

    ``text`` es lo que se vectoriza y lleva el significado. ``metadata`` viaja
    con el chunk pero NO se vectoriza: sirve para filtrar y citar la fuente.
    """

    chunk_id: str = Field(description="«{budget_id}::{component_id}»: de dónde sale el chunk.")
    text: str = Field(min_length=1, description="Texto que se vectoriza.")
    metadata: dict[str, MetadataValue] = Field(description="Datos para filtrar; no se vectorizan.")
    token_count: int = Field(ge=0, description="Tokens del texto, contados como lo hace OpenAI.")


class EmbeddedChunk(Chunk):
    """Un chunk con su vector: la lista de números que representa su significado."""

    embedding: list[float] = Field(min_length=1, description="El vector (1536 números con el modelo por defecto).")


class IngestRequest(_Model):
    """Lo que recibe ``POST /embeddings/ingest``: una lista de presupuestos."""

    budgets: list[Budget] = Field(min_length=1, description="Presupuestos a trocear y vectorizar.")

    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
        # Ejemplo que rellena el formulario de /docs («Try it out»).
        json_schema_extra={
            "examples": [
                {
                    "budgets": [
                        {
                            "budget_id": "BUD-2024-901",
                            "client_metadata": {"name": "Cliente de ejemplo", "sector": "finance", "country": "ES"},
                            "project_summary": "API de banca móvil con autenticación OAuth 2.0",
                            "main_technology": "ruby_on_rails",
                            "year": 2024,
                            "total_estimated_hours": 180,
                            "components": [
                                {
                                    "component_id": "AUTH-001",
                                    "name": "Backend de autenticación OAuth 2.0",
                                    "description": "Flujos OAuth 2.0 con sesiones basadas en JWT y límite de peticiones.",
                                    "tech_stack": ["ruby_on_rails", "postgresql", "redis"],
                                    "estimated_hours": 120,
                                    "complexity": "high",
                                    "dependencies": [],
                                },
                                {
                                    "component_id": "NOTIF-002",
                                    "name": "Notificaciones push y por email",
                                    "description": "Avisos de transacciones y de seguridad por push y correo.",
                                    "tech_stack": ["ruby_on_rails", "redis"],
                                    "estimated_hours": 60,
                                    "complexity": "low",
                                    "dependencies": ["AUTH-001"],
                                },
                            ],
                        }
                    ]
                }
            ]
        },
    )

    @model_validator(mode="after")
    def check_budget_ids_are_unique(self) -> "IngestRequest":
        """Dos presupuestos con el mismo id darían chunk_id repetidos."""
        ids = [budget.budget_id for budget in self.budgets]
        repeated = sorted({item for item in ids if ids.count(item) > 1})
        if repeated:
            raise ValueError(f"budget_id repetidos en la petición: {', '.join(repeated)}.")
        return self


class IngestStats(_Model):
    """Las cifras de una ingesta: cuánto se procesó y cuánto costó."""

    total_budgets: int = Field(ge=0, description="Presupuestos recibidos.")
    total_chunks: int = Field(ge=0, description="Chunks generados (uno por componente).")
    total_tokens: int = Field(ge=0, description="Tokens de todos los textos vectorizados.")
    estimated_cost_usd: float = Field(ge=0, description="Coste estimado de los embeddings, en dólares.")


class IngestResponse(_Model):
    """Lo que devuelve ``POST /embeddings/ingest``: los chunks con vector y las cifras."""

    chunks: list[EmbeddedChunk]
    stats: IngestStats
