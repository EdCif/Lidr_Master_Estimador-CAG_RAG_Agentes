"""Punto de entrada de la API y de la interfaz web del estimador."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT, get_settings
from app.context.examples import ESTIMATION_EXAMPLES
from app.errors import install_error_handlers
from app.routers.estimations import router as estimations_router
from app.routers.ideas import router as ideas_router
from app.routers.sessions import router as sessions_router
from app.services.idea_service import init_store, recover_running_runs


settings = get_settings()
WEB_DIR = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # El ejercicio utiliza un único proceso de servidor y una base SQLite local.
    await asyncio.to_thread(init_store, settings.data_dir)
    await asyncio.to_thread(recover_running_runs, settings.data_dir)
    yield

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)
install_error_handlers(app)
app.include_router(estimations_router)
app.include_router(ideas_router)
# Sesión 05: conversaciones con historial y memoria (POST /sessions...).
app.include_router(sessions_router)
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


@app.get("/api/v1/meta", tags=["system"])
def metadata():
    return {
        "app_name": settings.app_name,
        "model": settings.llm_model,
        "provider": settings.llm_provider,
        "reference_count": len(ESTIMATION_EXAMPLES),
    }


@app.get("/api/v1/sample-transcription", tags=["system"])
def sample_transcription():
    sample = PROJECT_ROOT / "docs" / "transcripcion_reunion.md"
    if not sample.is_file():
        raise HTTPException(status_code=404, detail="No se encuentra la reunión de ejemplo.")
    return {
        "title": "Portal de reservas",
        "description": "Una idea de ejemplo para probar el flujo completo de estimación.",
        "transcription": sample.read_text(encoding="utf-8"),
    }
