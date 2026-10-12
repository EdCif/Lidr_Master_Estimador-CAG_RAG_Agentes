"""Configuración centralizada, cargada desde el entorno y el archivo .env.

Uso desde otros módulos: ``from app.config import get_settings``.
La configuración se valida la primera vez que se llama a ``get_settings()``.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

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

    # Sesión 2 · Opcional: clave de Anthropic para la interfaz Streamlit
    # (streamlit_app.py llama a Claude Sonnet 5 a través de litellm).
    # Es opcional (None por defecto) para que la app FastAPI de la sesión 1
    # siga arrancando aunque solo exista OPENAI_API_KEY.
    anthropic_api_key: SecretStr | None = None

    # Sesión 05 · Conversaciones con memoria y adjuntos (/sessions/...).
    # Los dos caminos de adjuntos usan el mismo modelo, Claude Opus 5.5, para
    # que al compararlos la única diferencia sea cómo llegan los documentos.
    session_llm_model: str = Field(default="claude-opus-5-5", min_length=1)
    # Cuánto "piensa" Opus 5.5 antes de contestar. Su valor por defecto ya es
    # "medium"; lo dejamos escrito para que se vea y se pueda cambiar.
    session_llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Techo de tokens de salida (el razonamiento interno también cuenta).
    # Solo se cobra lo que se genera, así que un techo alto no encarece nada.
    session_max_tokens: int = Field(default=64000, ge=1024)
    # Ventana deslizante del historial: cuántos turnos (pregunta + respuesta)
    # viajan como máximo en cada llamada al modelo, contando la pregunta nueva.
    # Los más antiguos se descartan; la memoria (project_metadata) no.
    max_turns: int = Field(default=6, ge=1)
    # Cuántas sesiones caben a la vez en memoria. Al superarlo se borra la que
    # lleva más tiempo sin usarse (LRU), para no llenar la RAM del proceso.
    max_sessions: int = Field(default=500, ge=1)
    # Dirección de la API de sesiones a la que habla Streamlit (Paso 04). Por
    # defecto, la que arranca "uvicorn app.main:app --port 8001" en local.
    api_base_url: str = "http://127.0.0.1:8001"
    # Caducidad de las sesiones (TTL): segundos sin actividad tras los que una
    # sesión se archiva. El cliente puede entonces empezar otra de cero o
    # heredando su memoria. Mínimo 60 para no caducar en mitad de un turno.
    session_ttl_seconds: int = Field(default=3600, ge=60)
    # Camino de adjuntos por defecto: "local" (B, extraemos el texto aquí) o
    # "files_api" (A, el PDF se sube a Anthropic y Claude lo lee entero).
    # Cada petición puede elegir otro con el campo attachment_mode.
    attachment_mode: Literal["files_api", "local"] = "local"
    # Camino B: con qué librería sacamos el texto de los PDF. pypdf es rápida
    # y va bien con PDF de texto normales; MarkItDown devuelve Markdown con
    # más estructura en documentos complejos. Si pypdf no saca casi nada, se
    # prueba automáticamente con MarkItDown.
    pdf_extractor: Literal["pypdf", "markitdown"] = "pypdf"
    # Límites de los adjuntos, para no tumbar el servidor ni disparar la
    # factura de tokens con un documento enorme.
    max_attachments: int = Field(default=5, ge=0)
    max_attachment_mb: float = Field(default=10, gt=0)
    max_attachment_chars: int = Field(default=300_000, ge=1000)
    # Topes que se miran ANTES de abrir el fichero (ver attachments/common.py):
    # un PDF corto en megas puede tener miles de páginas, y un Word (que es un
    # ZIP) de 10 MB puede ocupar gigas al descomprimirlo.
    max_pdf_pages: int = Field(default=300, ge=1)
    max_docx_uncompressed_mb: float = Field(default=50, gt=0)
    # Camino B: tiempo máximo para extraer el texto de los adjuntos. La
    # extracción va en un proceso aparte que se mata si se pasa (ver
    # attachments/extraction_worker.py). Con 0 se extrae en la propia API,
    # sin proceso aparte ni límite: solo para depurar.
    attachment_extraction_timeout_s: float = Field(default=30, ge=0)

    @field_validator("openai_api_key", "llm_model", mode="before")
    @classmethod
    def strip_openai_values(cls, value: object) -> object:
        """Elimina espacios exteriores y permite detectar valores en blanco."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def blank_anthropic_key_is_none(cls, value: object) -> object:
        """Una ANTHROPIC_API_KEY vacía o con solo espacios equivale a no tenerla."""
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

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
    print("Clave Anthropic cargada:", settings.anthropic_api_key is not None)
