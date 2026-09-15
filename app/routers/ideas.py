"""HTTP para las ideas y su historial; el proveedor se llama fuera de SQLite."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.config import get_settings
from app.schemas.estimation import EstimationResponse
from app.schemas.idea import IdeaCreate, IdeaDetail, IdeaSummary, IdeaUpdate
from app.services import idea_service as store
from app.services.llm_service import generate_estimation

router = APIRouter(prefix="/api/v1/ideas", tags=["ideas"])


def get_data_dir() -> Path:
    return get_settings().data_dir


def _call(function, *args):
    try:
        return function(*args)
    except store.IdeaNotFound:
        raise HTTPException(404, "No se ha encontrado esta idea.") from None
    except store.EmptyTranscription:
        raise HTTPException(422, "Añade una transcripción antes de estimar.") from None
    except store.EstimationInProgress:
        raise HTTPException(409, "Esta idea ya tiene una estimación en curso.") from None


@router.get("", response_model=list[IdeaSummary])
def list_ideas(data_dir: Path = Depends(get_data_dir)):
    return store.list_ideas(data_dir)


@router.post("", response_model=IdeaDetail, status_code=201)
def create_idea(request: IdeaCreate, data_dir: Path = Depends(get_data_dir)):
    return store.create_idea(data_dir, request.model_dump())


@router.get("/{idea_id}", response_model=IdeaDetail)
def get_idea(idea_id: str, data_dir: Path = Depends(get_data_dir)):
    return _call(store.get_idea, data_dir, idea_id)


@router.patch("/{idea_id}", response_model=IdeaDetail)
def update_idea(idea_id: str, request: IdeaUpdate, data_dir: Path = Depends(get_data_dir)):
    return _call(store.update_idea, data_dir, idea_id, request.model_dump(exclude_unset=True))


@router.post("/{idea_id}/estimate", response_model=IdeaDetail)
async def estimate_idea(idea_id: str, data_dir: Path = Depends(get_data_dir)):
    run = await asyncio.to_thread(_call, store.start_run, data_dir, idea_id)
    try:
        raw_result = await generate_estimation(run["transcription"])
        result = EstimationResponse.model_validate(raw_result).model_dump()
    except (Exception, asyncio.CancelledError):
        await asyncio.shield(asyncio.to_thread(store.finish_run, data_dir, run["id"]))
        raise
    await asyncio.to_thread(store.finish_run, data_dir, run["id"], result)
    return await asyncio.to_thread(store.get_idea, data_dir, idea_id)
