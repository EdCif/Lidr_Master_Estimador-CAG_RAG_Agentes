"""Logger que escribe cada evento como una línea JSON en la consola.

Lo usan el observador de turnos (sesión 6) y el pipeline de embeddings
(sesión 7). Antes esta configuración vivía dentro de ``turn_observer.py``; se
saca a un fichero propio para no copiarla en cada módulo que quiera dejar un
registro con el mismo formato.
"""

import logging
import sys
from typing import Any

import structlog


def make_json_logger(name: str) -> Any:
    """Prepara un logger de structlog que escribe una línea JSON por evento.

    ``name`` es el nombre del logger estándar que hay debajo (por ejemplo
    ``"turn_observed"`` o ``"embedding_chunker"``). Sirve para encontrar sus
    líneas, y los tests lo usan para capturarlas.

    structlog construye el evento (nombre, campos y nivel) y lo convierte en
    una línea JSON; después se lo pasa al ``logging`` estándar de Python, que
    es quien lo escribe. Así no tocamos la configuración global de structlog ni
    la de los logs de uvicorn: solo estos loggers escriben JSON.

    uvicorn no configura el logging de nuestra aplicación, así que sin un
    "handler" propio estas líneas no saldrían por la consola. Le ponemos uno
    que escribe en la salida estándar (una sola vez, aunque el módulo se
    cargue dos veces).

    No añadimos la marca de hora de structlog (TimeStamper): cada módulo
    decide si su evento lleva hora (el observador de turnos tiene
    ``observed_at``) y así no se repite.
    """
    std_logger = logging.getLogger(name)
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
