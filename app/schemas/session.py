"""Datos que devuelven los endpoints de sesiones conversacionales (sesión 05)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.services.sessions import ProjectMetadata


class SessionCreated(BaseModel):
    """Respuesta de POST /sessions: solo el identificador de la sesión nueva.

    El cliente lo guarda y lo manda en cada petición posterior (va en la URL:
    /sessions/{session_id}/estimate). Mientras use el mismo identificador, el
    servidor sabrá qué historial y qué ficha de proyecto le corresponden.
    """

    session_id: str = Field(
        description="Identificador UUID v4 de la sesión.",
        examples=["3f2c8a6e-1b7d-4e0a-9c55-2d1f8e7b6a90"],
    )


class SessionState(BaseModel):
    """Foto de una sesión: su memoria y su historial, uno al lado del otro.

    No lo pide el enunciado, pero nos viene bien por dos motivos: Streamlit lo
    usará para pintar el panel de project_metadata, y así se ve con claridad
    que la memoria (una ficha corta) y el historial (los mensajes) son cosas
    distintas que viven por separado.
    """

    session_id: str
    created_at: datetime
    updated_at: datetime
    max_turns: int = Field(description="Tamaño de la ventana deslizante, en turnos.")
    turn_count: int = Field(description="Turnos completos que hay ahora en la ventana.")
    discarded_turns: int = Field(description="Turnos que la ventana ya ha descartado.")
    project_metadata: ProjectMetadata = Field(description="Memoria: hechos del proyecto.")
    history: list[dict[str, Any]] = Field(
        description="Historial: mensajes user/assistant dentro de la ventana, sin system prompt."
    )
