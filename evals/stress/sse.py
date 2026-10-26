"""Leer la respuesta en streaming (SSE) de ``POST /sessions/{id}/estimate``.

La API contesta cada turno como una serie de eventos ``status``, ``delta``,
``done`` o ``error``. En el protocolo SSE cada evento son unas líneas
"event: ..." y "data: ..." separadas del siguiente por una línea en blanco;
las líneas que empiezan por ":" son comentarios (los pings de FastAPI).

Es la misma función que ``tests/api_helpers.parse_sse``, copiada aquí a
propósito: la carpeta ``tests/`` no es un paquete de Python y el runner no
puede importarla (ver ``automejora.md``).
"""

from __future__ import annotations

import json
from typing import Any


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Convierte el texto de un stream SSE en una lista de (evento, datos)."""
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        name, data_lines = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events
