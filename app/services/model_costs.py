"""Cuánto cuesta una llamada al modelo (sesión 06).

Para qué sirve
--------------
El ejercicio de la sesión 6 mide cuánto cuesta cada turno de la
conversación (``cost_usd`` en el evento ``turn_observed``). El proveedor
nos dice cuántos tokens ha leído y escrito el modelo, pero no cuánto
cuestan: eso lo calculamos aquí con una tabla de precios.

¿Por qué una tabla propia y no la de litellm? litellm trae una tabla de
precios, pero no conoce los modelos que usamos (Claude Opus 5.5 y Haiku 5.5
salieron después de su última actualización). Una tabla pequeña y visible
es además más fácil de revisar: si Anthropic cambia un precio, se cambia
una línea.

Cómo se cobra
-------------
Los precios van en dólares por millón de tokens, separados en entrada (lo
que le mandamos: system prompt, historial, transcripción y adjuntos) y
salida (lo que escribe; en estos modelos incluye también el razonamiento
interno, que no vemos pero se paga).

Algunos modelos cambian de precio según lo larga que sea la petición: Haiku
5.5 cuesta 0,10 / 0,50 $ hasta 100.000 tokens de entrada y 0,50 / 2,50 $
por encima. Por eso cada modelo tiene una lista de tramos (``PriceTier``) y
la petición entera se cobra al precio del tramo en el que cae.

Precios de la documentación de Anthropic consultada en octubre de 2026.
No usamos la caché de prompts, así que no hace falta su precio aparte.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceTier:
    """Un tramo de precio: hasta cuántos tokens de entrada aplica y cuánto cuesta.

    ``up_to_input_tokens=None`` significa "sin tope": es el último tramo.
    """

    up_to_input_tokens: int | None
    input_usd_per_million: float
    output_usd_per_million: float


# Modelo -> tramos de precio, de menor a mayor tamaño de petición.
MODEL_COSTS: dict[str, tuple[PriceTier, ...]] = {
    "claude-opus-5-5": (PriceTier(None, 4.00, 20.00),),
    "claude-sonnet-5-5": (PriceTier(None, 2.00, 10.00),),
    "claude-haiku-5-5": (
        PriceTier(100_000, 0.10, 0.50),
        PriceTier(None, 0.50, 2.50),
    ),
}


def normalize_model(model: str | None) -> str | None:
    """Encuentra el modelo en la tabla aunque venga escrito de otra forma.

    * litellm le pone delante el proveedor: "anthropic/claude-haiku-5-5".
    * La API puede devolver una versión con fecha: "claude-haiku-5-5-20261001".

    Devuelve la clave de ``MODEL_COSTS`` o None si el modelo no está.
    """
    if not model:
        return None
    name = model.strip().lower().removeprefix("anthropic/")
    if name in MODEL_COSTS:
        return name
    for known in MODEL_COSTS:
        if name.startswith(known + "-"):
            return known
    return None


def estimate_cost_usd(model: str | None, tokens_in: int | None, tokens_out: int | None) -> float | None:
    """Coste en dólares de una llamada, o None si no se puede calcular.

    Devolvemos None (y no 0) cuando falta el dato: si el proveedor no nos
    dijo los tokens, o el modelo no está en la tabla, un 0 haría creer que
    el turno salió gratis y falsearía la curva de coste del informe.

    Ejemplo: Haiku 5.5 con 50.000 tokens de entrada y 2.000 de salida
    = 50.000 × 0,10 / 1.000.000 + 2.000 × 0,50 / 1.000.000 = 0,006 $.
    """
    key = normalize_model(model)
    if key is None or tokens_in is None or tokens_out is None:
        return None
    for tier in MODEL_COSTS[key]:
        if tier.up_to_input_tokens is None or tokens_in <= tier.up_to_input_tokens:
            cost = (
                tokens_in * tier.input_usd_per_million + tokens_out * tier.output_usd_per_million
            ) / 1_000_000
            return round(cost, 6)
    return None  # no se llega aquí: el último tramo siempre es "sin tope"
