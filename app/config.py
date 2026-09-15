"""Configuración centralizada, cargada desde el entorno y el archivo .env.

Uso desde otros módulos: ``from app.config import get_settings``.
La configuración se valida la primera vez que se llama a ``get_settings()``.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Variable de ruta.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Variables de la aplicación; el entorno tiene prioridad sobre .env."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    # Ajustes generales de la aplicación.
    app_name: str = "Estimador CAG"
    app_version: str = "0.1.0"
    app_env: str = "development"
    log_level: str = "DEBUG" # variable indica cuánto detalle queremos en los registros de la aplicación (logs),
    debug: bool = Field(default=False, validation_alias="APP_DEBUG")
    # Carpeta local para las ideas y su historial (no se sube al repositorio).
    data_dir: Path = PROJECT_ROOT / "data"
    llm_provider: str = "openai"
    # LLM_MODEL en .env sobrescribe el valor por defecto; OPENAI_MODEL también se admite.
    llm_model: str = Field(
        default="gpt-4o-mini",
        min_length=1,
        validation_alias=AliasChoices("llm_model", "openai_model"),
    )

    # Obligatorio: definir OPENAI_API_KEY en el entorno o .env.
    # SecretStr oculta la clave al imprimir o representar la configuración.
    openai_api_key: SecretStr = Field(min_length=1)

    @field_validator("openai_api_key", "llm_model", mode="before")
    @classmethod
    def strip_openai_values(cls, value: object) -> object:
        """Elimina espacios exteriores y permite detectar valores en blanco."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("data_dir")
    @classmethod
    def resolve_data_dir(cls, value: Path) -> Path:
        """Las rutas relativas se interpretan desde la raíz del proyecto."""
        return value if value.is_absolute() else PROJECT_ROOT / value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Carga y reutiliza la configuración; reinicia la app tras cambiar .env."""
    return Settings()

# Esta configuracion se basa en  Run Code - comprobación por pantalla
if __name__ == "__main__":
    # Se obtiene la configuración y se muestra.
    settings = get_settings()
    print("Proveedor:", settings.llm_provider)
    print("Modelo:", settings.llm_model)
    print("Clave cargada:", bool(settings.openai_api_key.get_secret_value()))
