"""Datos que recibe y devuelve el endpoint de estimaciones."""

from pydantic import BaseModel, ConfigDict, Field


class EstimationRequest(BaseModel):
    """Petición con la transcripción de la reunión que se quiere estimar."""

    model_config = ConfigDict(str_strip_whitespace=True)

    transcription: str = Field(
        min_length=1,
        description="Transcripción de la reunión con los requisitos del proyecto.",
        examples=[
            "Necesitamos una aplicación para consultar disponibilidad y gestionar reservas."
        ],
    )


class EstimationResponse(BaseModel):
    """Estimación generada y datos del modelo utilizado."""

    estimation: str = Field(min_length=1, description="Texto de la estimación generada.")
    model: str = Field(min_length=1, description="Modelo utilizado para la estimación.")
    provider: str = Field(min_length=1, description="Proveedor del modelo de lenguaje.")
