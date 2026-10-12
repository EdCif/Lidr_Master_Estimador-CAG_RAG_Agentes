"""Datos que devuelven los endpoints de sesiones conversacionales (sesión 05)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.services.sessions import ProjectMetadata


class SessionCreateRequest(BaseModel):
    """Cuerpo opcional de POST /sessions.

    Sin cuerpo (o con ``inherit_from`` vacío) se crea una sesión de cero.
    Con ``inherit_from`` la sesión nueva hereda la memoria (la ficha del
    proyecto) de otra sesión, normalmente una que ha caducado. El historial
    siempre empieza vacío.
    """

    model_config = ConfigDict(extra="forbid")

    inherit_from: UUID | None = Field(
        default=None,
        description="session_id de la sesión cuya memoria (project_metadata) se hereda.",
    )


class SessionCreated(BaseModel):
    """Respuesta de POST /sessions: el identificador de la sesión nueva y su memoria inicial.

    El cliente guarda el identificador y lo manda en cada petición posterior
    (va en la URL: /sessions/{session_id}/estimate). Mientras use el mismo
    identificador, el servidor sabrá qué historial y qué ficha le corresponden.
    """

    session_id: str = Field(
        description="Identificador UUID v4 de la sesión.",
        examples=["3f2c8a6e-1b7d-4e0a-9c55-2d1f8e7b6a90"],
    )
    inherited_from: str | None = Field(
        default=None, description="Sesión de la que se heredó la memoria, si es el caso."
    )
    project_metadata: ProjectMetadata = Field(
        default_factory=ProjectMetadata,
        description="Memoria con la que empieza: vacía, o la heredada.",
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
    expires_at: datetime = Field(description="Cuándo caducará si no hay más actividad (TTL).")
    inherited_from: str | None = Field(default=None, description="Sesión de la que heredó la memoria.")
    max_turns: int = Field(description="Tamaño de la ventana deslizante, en turnos.")
    turn_count: int = Field(description="Turnos completos que hay ahora en la ventana.")
    discarded_turns: int = Field(description="Turnos que la ventana ya ha descartado.")
    project_metadata: ProjectMetadata = Field(description="Memoria: hechos del proyecto.")
    history: list[dict[str, Any]] = Field(
        description="Historial: mensajes user/assistant dentro de la ventana, sin system prompt."
    )
    uploaded_files: list[dict[str, str]] = Field(
        default_factory=list,
        description="Camino A: ficheros subidos a la Files API (nombre y file_id).",
    )
