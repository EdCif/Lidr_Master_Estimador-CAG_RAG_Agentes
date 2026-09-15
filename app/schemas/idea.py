"""Contratos para guardar ideas y consultar sus estimaciones anteriores."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

IdeaStatus = Literal["draft", "in_review", "planned", "done"]


class IdeaCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    transcription: str = Field(default="", max_length=200000)
    notes: str = Field(default="", max_length=20000)


class IdeaUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20000)
    transcription: str | None = Field(default=None, max_length=200000)
    status: IdeaStatus | None = None
    notes: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="before")
    @classmethod
    def reject_null_values(cls, values):
        if isinstance(values, dict) and any(value is None for value in values.values()):
            raise ValueError("Omite los campos que no quieras modificar; no uses null.")
        return values


class EstimationRun(BaseModel):
    id: UUID
    idea_id: UUID
    state: Literal["running", "completed", "failed"]
    transcription: str
    estimation: str | None
    model: str | None
    provider: str | None
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class IdeaSummary(IdeaCreate):
    id: UUID
    status: IdeaStatus
    created_at: datetime
    updated_at: datetime
    latest_run: EstimationRun | None
    run_count: int


class IdeaDetail(IdeaSummary):
    runs: list[EstimationRun]
