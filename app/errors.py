"""Traduce los errores del servicio a respuestas HTTP sin detalles internos."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import APITimeoutError, OpenAIError, RateLimitError


async def _service_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, APITimeoutError):
        status_code = 504
        detail = "El proveedor de IA ha tardado demasiado en responder. Inténtalo de nuevo."
    elif isinstance(exc, RateLimitError):
        status_code = 503
        detail = "El proveedor de IA no está disponible temporalmente. Inténtalo más tarde."
    elif isinstance(exc, (OpenAIError, RuntimeError)):
        status_code = 502
        detail = "No se ha podido obtener una estimación del proveedor de IA."
    else:
        status_code = 500
        detail = "No se ha podido procesar la estimación."

    return JSONResponse(status_code=status_code, content={"detail": detail})


def install_error_handlers(app: FastAPI) -> None:
    """Instala los errores comunes y conserva la validación y los errores HTTP."""
    for error_type in (
        APITimeoutError,
        RateLimitError,
        OpenAIError,
        RuntimeError,
        ValueError,
    ):
        app.add_exception_handler(error_type, _service_error_handler)
