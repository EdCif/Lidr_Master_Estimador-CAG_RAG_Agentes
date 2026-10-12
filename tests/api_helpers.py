"""Utilidades compartidas por los tests de la API de sesiones.

No empieza por "test_", así que ni unittest ni pytest lo ejecutan como
pruebas: solo lo importan los tests que lo necesitan.
"""

import json
from types import SimpleNamespace

from pydantic import SecretStr


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Convierte el texto de un stream SSE en una lista de (evento, datos).

    En el protocolo SSE cada evento son unas líneas "event: ..." y
    "data: ..." separadas del siguiente por una línea en blanco. Las líneas
    que empiezan por ":" son comentarios (los pings de FastAPI) y se ignoran.
    """
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


def fake_settings(**overrides):
    """Los ajustes que leen los endpoints, sin tocar el .env real."""
    values = dict(
        anthropic_api_key=SecretStr("test-key-not-a-real-credential"),
        session_llm_model="claude-opus-5-5",
        session_llm_effort="medium",
        session_max_tokens=64000,
        attachment_mode="local",
        pdf_extractor="pypdf",
        max_attachments=5,
        max_attachment_mb=10,
        max_attachment_chars=300_000,
        max_pdf_pages=300,
        max_docx_uncompressed_mb=50,
        attachment_extraction_timeout_s=30,
        max_turns=6,
        max_sessions=500,
        session_ttl_seconds=3600,
    )
    values.update(overrides)
    return SimpleNamespace(**values)
