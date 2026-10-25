"""Las tres métricas del stress test (sesión 6): ¿pasa o no pasa este turno?

Qué es una métrica aquí
-----------------------
Un "juez" que mira un turno y contesta con un resultado (``MetricResult``):

* ``name``: cómo se llama la métrica.
* ``score``: 1.0 si pasa, 0.0 si no. Son binarias: nada de "un 0,7".
* ``passed``: lo mismo, como verdadero o falso.
* ``details``: los números que explican el veredicto (cuánto tardó, cuál era
  el tope, dónde se encontró el hecho...). Van al CSV para poder revisarlos.

Por qué binarias y con presupuesto
----------------------------------
Un presupuesto ("como mucho 20 segundos", "como mucho 5 céntimos") es una
promesa de diseño, no un número que se mira después a ver qué tal. Al
convertirlo en una métrica que pasa o no pasa, la promesa se vuelve una
prueba que puede ponerse en rojo. Y las métricas no se promedian entre sí:
un turno rapidísimo que se ha olvidado del proyecto no "compensa".

Lo mismo dicen las lecciones de la sesión: el objeto ``CAGViability`` son
cuatro booleanos con ``all([...])``, y "basta con que uno falle para que la
arquitectura no sea viable".

Fallar duro frente a degradar en silencio
-----------------------------------------
Las dos primeras métricas vigilan cosas que, si fallan, se notan (el
usuario espera o la factura sube). La tercera, ``MemoryDriftMetric``,
vigila lo peligroso de verdad en un sistema CAG: que el modelo siga
contestando con aplomo pero se haya olvidado de un hecho que se dijo turnos
atrás. Eso no lanza ninguna excepción; solo una métrica lo ve.

Determinismo
------------
Nada de embeddings ni de otra IA haciendo de juez: buscamos el texto exacto
del hecho, sin distinguir mayúsculas. Así el mismo turno da siempre el mismo
veredicto y el informe se puede repetir.

Por qué en este módulo y no en un ``evals/metrics.py`` general
-------------------------------------------------------------
El enunciado deja elegir. Estas métricas leen la observación del turno
(``turn_observed``) y la foto de la sesión (``GET /sessions/{id}``), no una
estimación suelta: dependen de la forma de esos datos y solo tienen sentido
dentro del stress test. Por eso viven junto a él.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.services.turn_observer import render_metadata_facts

# Dónde puede buscar MemoryDriftMetric un hecho dentro de la foto de la sesión.
Location = Literal["metadata", "history", "reply"]
LOCATIONS: tuple[Location, ...] = ("metadata", "history", "reply")

# Presupuestos por defecto. Los usan el panel de Streamlit y el runner del
# stress test, para que los dos juzguen igual; ambos dejan cambiarlos.
#
# * Latencia: 20 segundos por turno. El enunciado pone de ejemplo 4 s, pero
#   nuestra respuesta es una estimación completa (un documento largo): con
#   Haiku 5.5 ya tarda unos 15 s, y con 4 s no pasaría ningún turno. Con el
#   streaming el cliente ve texto antes (ttft_ms), pero el turno no está
#   listo hasta el final.
# * Coste: 3 céntimos por turno. Es la cifra de "desplegable" que pone la
#   lección de calidad del dato de la sesión (3 céntimos frente a 2 euros).
DEFAULT_LATENCY_BUDGET_MS = 20_000
DEFAULT_COST_BUDGET_USD = 0.03


@dataclass(frozen=True)
class MetricResult:
    """El veredicto de una métrica sobre un turno."""

    name: str
    score: float
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)


def _verdict(name: str, passed: bool, details: dict[str, Any]) -> MetricResult:
    return MetricResult(name=name, score=1.0 if passed else 0.0, passed=passed, details=details)


# ---------------------------------------------------------------------------
# 1. Presupuesto de latencia
# ---------------------------------------------------------------------------

class LatencyBudgetMetric:
    """1.0 si el turno tardó ``budget_ms`` o menos; 0.0 si tardó más.

    Lee ``latency_ms`` de la observación del turno: lo que tardó la API
    desde que empezó el turno hasta justo antes de la respuesta final.

    Ejemplo: con ``budget_ms=20000``, un turno de 15.400 ms pasa (le
    sobran 4.600 ms) y uno de 23.000 ms no pasa.
    """

    name = "latency_budget"
    reads = "observation"

    def __init__(self, budget_ms: int) -> None:
        if budget_ms <= 0:
            raise ValueError("budget_ms tiene que ser mayor que 0.")
        self.budget_ms = budget_ms

    def evaluate(self, observation: Mapping[str, Any]) -> MetricResult:
        latency = observation.get("latency_ms")
        if latency is None:
            return _verdict(self.name, False, {"budget_ms": self.budget_ms, "reason": "falta latency_ms"})
        return _verdict(self.name, latency <= self.budget_ms, {
            "latency_ms": latency,
            "budget_ms": self.budget_ms,
            "margin_ms": self.budget_ms - latency,
        })


# ---------------------------------------------------------------------------
# 2. Presupuesto de coste
# ---------------------------------------------------------------------------

class CostBudgetMetric:
    """1.0 si el turno costó ``budget_usd`` o menos; 0.0 si costó más.

    Lee ``cost_usd`` de la observación. Si el coste no se conoce (el
    proveedor no dio los tokens), NO pasa: un presupuesto que no se puede
    comprobar no está cumplido. El motivo queda en ``details``.

    Ejemplo: con ``budget_usd=0.05``, un turno de 0,0011 $ pasa.
    """

    name = "cost_budget"
    reads = "observation"

    def __init__(self, budget_usd: float) -> None:
        if budget_usd <= 0:
            raise ValueError("budget_usd tiene que ser mayor que 0.")
        self.budget_usd = budget_usd

    def evaluate(self, observation: Mapping[str, Any]) -> MetricResult:
        cost = observation.get("cost_usd")
        if cost is None:
            return _verdict(self.name, False, {"budget_usd": self.budget_usd, "reason": "falta cost_usd"})
        return _verdict(self.name, cost <= self.budget_usd, {
            "cost_usd": cost,
            "budget_usd": self.budget_usd,
            "margin_usd": round(self.budget_usd - cost, 6),
        })


# ---------------------------------------------------------------------------
# 3. Deriva de memoria
# ---------------------------------------------------------------------------

def _message_text(content: Any) -> str:
    """El texto de un mensaje del historial.

    Normalmente es un texto. En los turnos del Camino A es una lista de
    bloques (documentos + texto): nos quedamos con los bloques de texto; el
    PDF lo leyó Anthropic y no está en el historial como texto.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, Mapping))
    return ""


def snapshot_texts(snapshot: Mapping[str, Any]) -> dict[Location, str]:
    """Los tres sitios donde puede seguir vivo un hecho, como texto.

    ``snapshot`` es lo que devuelve ``GET /sessions/{id}``.

    * ``metadata``: la memoria (``project_metadata``), escrita con las mismas
      líneas que ve el modelo en su system prompt ("Proyecto: Nimbus",
      "Equipo asumido: 3 personas"...). Sobrevive a la ventana.
    * ``history``: todos los mensajes que quedan en la ventana deslizante,
      del cliente y del modelo. Lo que salió de la ventana ya no está.
    * ``reply``: la última respuesta del modelo. Si un hecho aparece aquí, el
      modelo lo ha usado al estimar.

    El enunciado busca en "summary, anchors y metadata". Nosotros no tenemos
    resumen y nuestras anclas SON la memoria; por eso miramos la memoria, el
    historial y la última respuesta.
    """
    history = snapshot.get("history") or []
    replies = [_message_text(m.get("content")) for m in history if m.get("role") == "assistant"]
    return {
        "metadata": render_metadata_facts(snapshot.get("project_metadata")),
        "history": "\n\n".join(_message_text(m.get("content")) for m in history),
        "reply": replies[-1] if replies else "",
    }


class MemoryDriftMetric:
    """¿Sigue vivo en la sesión un hecho que se dijo turnos atrás?

    Busca ``fact`` en la foto de la sesión, en los sitios de ``where``
    (por defecto, los tres de ``snapshot_texts``).

    * Con ``expected=True`` (lo normal): 1.0 si el hecho aparece en alguno de
      esos sitios; 0.0 si se ha perdido. Ejemplo: "nimbus" dicho en el turno
      1, buscado en el turno 20.
    * Con ``expected=False``: 1.0 si el hecho YA NO aparece. Sirve para los
      cambios de rumbo: tras "descartad React", ¿la memoria lo ha olvidado o
      sigue arrastrándolo?

    Coincidencia exacta, sin distinguir mayúsculas y, por defecto, como
    palabra completa (``whole_word``): "sap" no cuenta dentro de
    "desaparece", pero sí en "integración con SAP".
    """

    name = "memory_drift"
    reads = "snapshot"

    def __init__(
        self,
        fact: str,
        where: Sequence[Location] = LOCATIONS,
        expected: bool = True,
        whole_word: bool = True,
    ) -> None:
        fact = fact.strip()
        if not fact:
            raise ValueError("El hecho a buscar no puede estar vacío.")
        unknown = [place for place in where if place not in LOCATIONS]
        if unknown or not where:
            raise ValueError(f"where solo admite {', '.join(LOCATIONS)} (y al menos uno): {list(where)}")
        self.fact = fact
        self.where = tuple(where)
        self.expected = expected
        # (?<!\w) y (?!\w): "que no haya una letra o número pegado delante ni
        # detrás". Es la forma de pedir "palabra completa" que funciona
        # también con hechos que empiezan o acaban en un signo, como "C#".
        escaped = re.escape(fact)
        pattern = rf"(?<!\w){escaped}(?!\w)" if whole_word else escaped
        self._pattern = re.compile(pattern, re.IGNORECASE)

    def evaluate(self, session_snapshot: Mapping[str, Any]) -> MetricResult:
        texts = snapshot_texts(session_snapshot)
        found_in = [place for place in self.where if self._pattern.search(texts[place])]
        found = bool(found_in)
        return _verdict(self.name, found == self.expected, {
            "fact": self.fact,
            "where": list(self.where),
            "expected": self.expected,
            "found_in": found_in,
        })


# ---------------------------------------------------------------------------
# 4. Pasar varias métricas de una vez
# ---------------------------------------------------------------------------

def run_all_metrics(
    metrics: Iterable[LatencyBudgetMetric | CostBudgetMetric | MemoryDriftMetric],
    *,
    observation: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> list[MetricResult]:
    """Evalúa cada métrica con lo que necesita.

    Cada métrica dice qué lee en su atributo ``reads``: la observación del
    turno (latencia, coste) o la foto de la sesión (deriva de memoria).
    """
    return [
        metric.evaluate(observation if metric.reads == "observation" else snapshot)
        for metric in metrics
    ]
