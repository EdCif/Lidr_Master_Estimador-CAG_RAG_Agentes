"""Capa de transporte HTTP para las estimaciones de proyectos."""
"""No construye prompts, no valida errores, no formatea."""
"""Solo recibe, delega y devuelve. Para tocar, modelos, proveedor o prompt en los otros ficheros."""

from fastapi import APIRouter, HTTPException, status
from openai import APITimeoutError, OpenAIError, RateLimitError

from app.schemas.estimation import EstimationRequest, EstimationResponse
from app.services.llm_service import generate_estimation


router = APIRouter(prefix="/api/v1", tags=["estimations"])


@router.post("/estimate",response_model=EstimationResponse)
async def estimate(request: EstimationRequest):
    """Valida la petición, llama al servicio LLM y devuelve su resultado."""
    result =await generate_estimation(request.transcription)
    return result
