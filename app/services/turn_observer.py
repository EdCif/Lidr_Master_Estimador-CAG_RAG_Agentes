"""La "ficha técnica" de cada turno: el evento ``turn_observed`` (sesión 06).

Para qué sirve
--------------
El ejercicio de la sesión 6 somete el estimador a conversaciones largas y a
documentos grandes para ver dónde empieza a fallar: si se dispara la
latencia, si el coste crece sin control o si se olvidan los hechos del
proyecto. Para medirlo, cada turno deja UNA observación con todo lo
relevante junto (tokens, coste, tiempo, tamaño de la ventana...).

¿Por qué un único evento y no varios logs sueltos? Porque así cada turno es
una fila: se puede volcar a un CSV de una pasada y comparar columnas (por
ejemplo, mensajes en la ventana frente a coste) sin tener que casar líneas
de log por su hora.

Quién lo usa
------------
* ``conversation_service.stream_estimation_turn`` llama a ``observe_turn``
  al terminar cada turno, también si acaba en error.
* La observación viaja en el evento SSE ``done`` (o ``error``) y se guarda en
  la sesión: ``GET /sessions/{id}`` la devuelve como ``last_turn_observed``.
* Además se escribe con structlog como una línea JSON en la consola de la
  API (``{"event": "turn_observed", ...}``), que es lo que pide el enunciado.
* El runner del stress test (``evals/stress/run.py``) la lee del ``done``.

Los 13 campos del enunciado y lo que valen en NUESTRO proyecto
--------------------------------------------------------------
El enunciado parte de un proyecto con piezas que el nuestro no tiene
(resumen acumulado, caché, elección de modelo por niveles). No las
inventamos: esos campos llevan un valor fijo que dice "aquí no hay nada".

* ``turn_index``: número del turno en la sesión, empezando en 1. Cuenta
  también los turnos que la ventana ya ha descartado.
* ``session_id``: la sesión.
* ``enriched_transcript_chars``: caracteres de la transcripción más los del
  texto de los adjuntos.
* ``attachments_total_chars``: caracteres de texto de los adjuntos (0 si no
  hay). En el Camino A los PDF los lee Anthropic y no sabemos su texto:
  solo cuentan los Word, que sí convertimos aquí.
* ``messages_in_window``: mensajes que quedan en el historial después de
  aplicar la ventana deslizante (como mucho 2 × MAX_TURNS).
* ``anchors_count``: nuestras "anclas" son los hechos de la memoria
  (``project_metadata``): nombre, equipo, cada tecnología y alcance.
* ``summary_chars``: siempre 0. No hay resumen acumulado; lo que ocupa
  nuestra memoria va en el extra ``metadata_chars``.
* ``tokens_in`` / ``tokens_out``: los que dice el proveedor. La salida
  incluye el razonamiento interno del modelo.
* ``cost_usd``: calculado con ``model_costs.estimate_cost_usd``.
* ``latency_ms``: desde que empieza el turno hasta justo antes del evento
  final (extraer adjuntos + llamada al modelo + actualizar la memoria).
* ``cache_hit_kind``: siempre "none" (no hay caché de respuestas).
* ``last_resolved_tier``: siempre "fixed:<modelo>" (el modelo no cambia
  según la petición).

Extras nuestros, para leer mejor las curvas: ``ttft_ms`` (tiempo hasta el
primer trozo de texto: refleja lo que tarda el modelo en "leer" lo que le
mandamos), ``llm_ms``, ``reply_chars``, ``metadata_chars`` y otros que se
explican en ``TurnObservation``.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, Field

from app.services.attachments.common import ReplyStats
from app.services.model_costs import estimate_cost_usd

if TYPE_CHECKING:
    # Solo para las anotaciones de tipo. sessions.py guarda una
    # TurnObservation, así que importarlo aquí "de verdad" sería circular.
    from app.services.sessions import ProjectMetadata, Session

# Nombre del evento y del logger: es lo que se busca en la consola.
TURN_OBSERVED = "turn_observed"

# Los 13 campos que exige el enunciado, en su orden. Los tests y el runner
# comprueban que están todos.
REQUIRED_FIELDS: tuple[str, ...] = (
    "turn_index",
    "session_id",
    "enriched_transcript_chars",
    "attachments_total_chars",
    "messages_in_window",
    "anchors_count",
    "summary_chars",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "latency_ms",
    "cache_hit_kind",
    "last_resolved_tier",
)


# ---------------------------------------------------------------------------
# 1. El cronómetro del turno
# ---------------------------------------------------------------------------

@dataclass
class TurnTimer:
    """Apunta los momentos clave de un turno para calcular tiempos.

    Usamos ``time.perf_counter()``, un reloj pensado para medir duraciones
    (no se ve afectado si cambia la hora del ordenador). Los tests pasan un
    reloj falso (``clock``) para que los tiempos sean exactos.

    Momentos que apuntamos:
    * ``started``: empieza el turno (al crear el cronómetro).
    * ``llm_started``: sale la petición al modelo.
    * ``first_delta``: llega el primer trozo de texto.
    * ``llm_ended``: termina el streaming del modelo.
    """

    clock: Callable[[], float] = time.perf_counter
    started: float = field(init=False)
    llm_started: float | None = field(default=None, init=False)
    first_delta: float | None = field(default=None, init=False)
    llm_ended: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def mark_llm_start(self) -> None:
        self.llm_started = self.clock()

    def mark_first_delta(self) -> None:
        # Solo cuenta el primero: se llama en cada trozo y los demás se ignoran.
        if self.first_delta is None:
            self.first_delta = self.clock()

    def mark_llm_end(self) -> None:
        self.llm_ended = self.clock()

    def elapsed_ms(self) -> int:
        """Milisegundos desde que empezó el turno hasta ahora."""
        return _ms(self.clock() - self.started)

    def ttft_ms(self) -> int | None:
        """Tiempo hasta el primer trozo de texto (time to first token)."""
        if self.llm_started is None or self.first_delta is None:
            return None
        return _ms(self.first_delta - self.llm_started)

    def llm_ms(self) -> int | None:
        """Lo que ha durado la llamada al modelo (hasta ahora, si se cortó)."""
        if self.llm_started is None:
            return None
        end = self.llm_ended if self.llm_ended is not None else self.clock()
        return _ms(end - self.llm_started)


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


# ---------------------------------------------------------------------------
# 2. La observación: los 13 campos del enunciado + extras nuestros
# ---------------------------------------------------------------------------

class TurnObservation(BaseModel):
    """Todo lo que sabemos de un turno al terminarlo. Ver el docstring del módulo."""

    # Los 13 campos del enunciado.
    turn_index: int = Field(description="Número del turno en la sesión (empieza en 1).")
    session_id: str
    enriched_transcript_chars: int = Field(description="Transcripción + texto de los adjuntos.")
    attachments_total_chars: int = Field(description="Texto de los adjuntos (0 si no hay).")
    messages_in_window: int = Field(description="Mensajes en el historial tras aplicar la ventana.")
    anchors_count: int = Field(description="Hechos en la memoria (project_metadata).")
    summary_chars: int = Field(default=0, description="Siempre 0: no hay resumen acumulado.")
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = Field(default=None, description="None si faltan tokens o precio.")
    latency_ms: int = Field(description="Duración del turno en el servidor.")
    cache_hit_kind: Literal["none", "exact", "semantic"] = Field(
        default="none", description="Siempre 'none': no hay caché de respuestas."
    )
    last_resolved_tier: str = Field(description="Siempre 'fixed:<modelo>': no hay tier dinámico.")

    # Extras nuestros.
    outcome: Literal["done", "error"] = Field(description="Cómo acabó el turno.")
    error_detail: str | None = None
    model: str = Field(description="Modelo que ha contestado.")
    attachment_mode: str = Field(description="Camino de los adjuntos: local (B) o files_api (A).")
    attachments_count: int = 0
    ttft_ms: int | None = Field(default=None, description="Hasta el primer trozo de texto.")
    llm_ms: int | None = Field(default=None, description="Duración de la llamada al modelo.")
    reply_chars: int = Field(default=0, description="Caracteres de la respuesta visible.")
    metadata_chars: int = Field(default=0, description="Lo que ocupa la memoria en el prompt.")
    turns_dropped: int = Field(default=0, description="Turnos que la ventana ya ha descartado.")
    messages_sent: int | None = Field(
        default=None, description="Mensajes enviados al modelo, system prompt incluido."
    )
    tokens_source: Literal["usage", "missing"] = Field(
        description="'usage' si el proveedor dio los tokens; 'missing' si no."
    )
    observed_at: datetime


# ---------------------------------------------------------------------------
# 3. La memoria vista como texto (la usan también las métricas)
# ---------------------------------------------------------------------------

def _as_dict(metadata: ProjectMetadata | Mapping[str, Any]) -> dict[str, Any]:
    """La ficha como diccionario, venga como objeto o ya como diccionario."""
    return metadata.model_dump() if hasattr(metadata, "model_dump") else dict(metadata)


def render_metadata_facts(metadata: ProjectMetadata | Mapping[str, Any] | None) -> str:
    """Escribe la memoria en las mismas líneas que ve el modelo en el prompt.

    Ejemplo:
        Proyecto: Nimbus
        Equipo asumido: 3 personas
        Tecnologías: React, FastAPI
        Alcance acordado: la primera fase no incluye pagos

    Son las líneas del bloque <project_metadata> de la plantilla
    ``app/prompts/estimator_system_v1.j2``. Acepta la ficha (``ProjectMetadata``)
    o el diccionario que devuelve ``GET /sessions/{id}``, porque la métrica de
    deriva de memoria (``evals/stress/metrics.py``) trabaja con ese diccionario.
    """
    if metadata is None:
        return ""
    data = _as_dict(metadata)
    lines = []
    if data.get("project_name"):
        lines.append(f"Proyecto: {data['project_name']}")
    if data.get("assumed_team_size"):
        lines.append(f"Equipo asumido: {data['assumed_team_size']} personas")
    if data.get("mentioned_technologies"):
        lines.append("Tecnologías: " + ", ".join(data["mentioned_technologies"]))
    if data.get("agreed_scope"):
        lines.append(f"Alcance acordado: {data['agreed_scope']}")
    return "\n".join(lines)


def count_anchors(metadata: ProjectMetadata | Mapping[str, Any] | None) -> int:
    """Cuántos hechos guarda la memoria: nombre, equipo, cada tecnología y alcance."""
    if metadata is None:
        return 0
    data = _as_dict(metadata)
    return (
        bool(data.get("project_name"))
        + bool(data.get("assumed_team_size"))
        + len(data.get("mentioned_technologies") or [])
        + bool(data.get("agreed_scope"))
    )


# ---------------------------------------------------------------------------
# 4. El registro con structlog
# ---------------------------------------------------------------------------

def _make_logger() -> Any:
    """Prepara el logger de structlog que escribe una línea JSON por turno.

    structlog construye el evento (nombre, campos y nivel) y lo convierte en
    una línea JSON; después se lo pasa al ``logging`` estándar de
    Python, que es quien lo escribe. Así no tocamos la configuración global
    de structlog ni la de los logs de uvicorn: solo este logger escribe JSON.

    uvicorn no configura el logging de nuestra aplicación, así que sin un
    "handler" propio estas líneas no saldrían por la consola. Le ponemos uno
    que escribe en la salida estándar (una sola vez, aunque el módulo se
    cargue dos veces).

    No añadimos la marca de hora de structlog (TimeStamper): la observación
    ya lleva la suya, ``observed_at``, y estaría repetida.
    """
    std_logger = logging.getLogger(TURN_OBSERVED)
    std_logger.setLevel(logging.INFO)
    if not std_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        std_logger.addHandler(handler)
    return structlog.wrap_logger(
        std_logger,
        wrapper_class=structlog.stdlib.BoundLogger,
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )


_log = _make_logger()


# ---------------------------------------------------------------------------
# 5. Lo que llama el servicio al final de cada turno
# ---------------------------------------------------------------------------

def observe_turn(
    session: Session,
    *,
    turn_index: int,
    transcript: str,
    attachment_chars: int,
    attachments_count: int,
    stats: ReplyStats,
    attachment_mode: str,
    configured_model: str,
    timer: TurnTimer,
    messages_sent: int | None,
    outcome: Literal["done", "error"],
    error_detail: str | None = None,
) -> TurnObservation:
    """Construye la observación del turno, la guarda en la sesión y la registra.

    Se llama justo antes del evento final (``done`` o ``error``), cuando el
    historial y la memoria ya están actualizados: así ``messages_in_window``
    y ``anchors_count`` describen la sesión tal como queda tras el turno.
    """
    tokens_known = stats.input_tokens is not None and stats.output_tokens is not None
    observation = TurnObservation(
        turn_index=turn_index,
        session_id=session.session_id,
        enriched_transcript_chars=len(transcript) + attachment_chars,
        attachments_total_chars=attachment_chars,
        messages_in_window=len(session.history.messages),
        anchors_count=count_anchors(session.metadata),
        summary_chars=0,
        tokens_in=stats.input_tokens,
        tokens_out=stats.output_tokens,
        cost_usd=estimate_cost_usd(stats.model, stats.input_tokens, stats.output_tokens),
        latency_ms=timer.elapsed_ms(),
        cache_hit_kind="none",
        last_resolved_tier=f"fixed:{configured_model}",
        outcome=outcome,
        error_detail=error_detail,
        model=stats.model,
        attachment_mode=attachment_mode,
        attachments_count=attachments_count,
        ttft_ms=timer.ttft_ms(),
        llm_ms=timer.llm_ms(),
        reply_chars=len(stats.final_text or ""),
        metadata_chars=len(render_metadata_facts(session.metadata)),
        turns_dropped=session.history.discarded_turns,
        messages_sent=messages_sent,
        tokens_source="usage" if tokens_known else "missing",
        observed_at=datetime.now(timezone.utc),
    )
    session.last_turn_observed = observation
    # Una línea JSON en la consola: {"event": "turn_observed", "turn_index": 3, ...}
    _log.info(TURN_OBSERVED, **observation.model_dump(mode="json"))
    return observation
