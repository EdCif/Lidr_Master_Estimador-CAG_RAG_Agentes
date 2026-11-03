"""El endpoint ``POST /embeddings/ingest``: la «puerta» del pipeline.

Un endpoint es una dirección web que espera datos y contesta. Este recibe
presupuestos y devuelve sus chunks ya vectorizados, uniendo las piezas que
construimos antes:

    presupuestos ──► moldes (schemas) ──► chunker ──► embedder ──► respuesta

El cuerpo del endpoint se lee como una receta de tres pasos: trocear,
vectorizar y montar la respuesta. Todo lo demás son los avisos de qué decir
cuando algo sale mal.

Qué códigos devuelve y por qué:

- **200**: todo bien. Los chunks con su vector y las estadísticas.
- **422**: la petición no es válida. Lo detecta FastAPI solo con los moldes
  (sector inventado, horas negativas...) o lo decidimos aquí si un componente
  es tan largo que no cabe en el modelo (el mensaje dice cuál hay que acortar).
- **503**: OpenAI pide esperar (límite de peticiones) y se agotaron los 3
  reintentos. Se puede reintentar más tarde.
- **500**: cualquier otro fallo. El cliente solo recibe un mensaje genérico;
  el detalle (tipo de error y mensaje) queda en el log del servidor.

Los errores se capturan aquí y no se dejan a ``app/errors.py``: esos
manejadores son de toda la aplicación y hablan de «estimación» (un 502 con
«no se ha podido obtener una estimación»), que aquí no tendría sentido.
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException
from openai import RateLimitError

from app.embedding_pipeline.chunker import ChunkTooLargeError, JSONStructuralChunker
from app.embedding_pipeline.embedder import OpenAIEmbedder, create_embedder, estimate_cost_usd
from app.embedding_pipeline.schemas import IngestRequest, IngestResponse, IngestStats
from app.services.json_logger import make_json_logger

LOGGER_NAME = "embedding_router"
_log = make_json_logger(LOGGER_NAME)

router = APIRouter(prefix="/embeddings", tags=["embeddings"])


def get_chunker() -> JSONStructuralChunker:
    """El chunker que usa el endpoint (no guarda estado, así que basta con uno nuevo).

    Se pide con ``Depends`` en vez de crearlo dentro del endpoint para poder
    sustituirlo en los tests con ``app.dependency_overrides``.
    """
    return JSONStructuralChunker()


@lru_cache(maxsize=1)
def get_embedder() -> OpenAIEmbedder:
    """El embedder real, con la clave y el modelo del ``.env``.

    ``lru_cache(maxsize=1)`` hace que se cree una sola vez y se reutilice (con
    su cliente de OpenAI y sus conexiones), igual que ``get_settings()``. Un
    cambio en el ``.env`` necesita reiniciar la API. Los tests lo sustituyen
    por uno falso con ``app.dependency_overrides``.
    """
    return create_embedder()


@router.post(
    "/ingest",
    response_model=IngestResponse,
    summary="Trocea presupuestos y devuelve sus chunks con los vectores",
    responses={
        422: {"description": "Presupuesto no válido, o un componente demasiado largo para el modelo."},
        500: {"description": "Error inesperado al generar los embeddings (el detalle queda en el log)."},
        503: {"description": "OpenAI pide esperar (límite de peticiones). Inténtalo más tarde."},
    },
)
def ingest(
    request: IngestRequest,
    chunker: JSONStructuralChunker = Depends(get_chunker),
    embedder: OpenAIEmbedder = Depends(get_embedder),
) -> IngestResponse:
    """Recibe presupuestos y devuelve un chunk vectorizado por cada componente.

    Es una función normal (``def``, no ``async def``): FastAPI la ejecuta en un
    hilo aparte, así que las esperas de los reintentos no bloquean al resto de
    la API.
    """
    budgets = request.budgets
    _log.info(
        "embeddings_ingest_received",
        budgets=len(budgets),
        components=sum(len(budget.components) for budget in budgets),
    )

    try:
        chunks = chunker.chunk(budgets)  # 1. trocear
        embedded = embedder.embed_many(chunks)  # 2. vectorizar
    except ChunkTooLargeError as error:
        _log.warning("embeddings_ingest_rejected", chunk_id=error.chunk_id, tokens=error.token_count)
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RateLimitError as error:
        _log.warning("embeddings_ingest_rate_limited", detail=_short(error))
        raise HTTPException(
            status_code=503,
            detail="El servicio de embeddings está saturado en este momento. Inténtalo más tarde.",
        ) from error
    except Exception as error:  # noqa: BLE001 - cualquier otro fallo es un 500 genérico
        _log.error(
            "embeddings_ingest_failed",
            error_type=type(error).__name__,
            status=getattr(error, "status_code", None),
            detail=_short(error),
        )
        raise HTTPException(status_code=500, detail="No se han podido generar los embeddings.") from error

    # 3. montar la respuesta
    total_tokens = sum(chunk.token_count for chunk in chunks)
    stats = IngestStats(
        total_budgets=len(budgets),
        total_chunks=len(chunks),
        total_tokens=total_tokens,
        estimated_cost_usd=estimate_cost_usd(total_tokens),
    )
    _log.info("embeddings_ingest_done", **stats.model_dump())
    return IngestResponse(chunks=embedded, stats=stats)


def _short(error: Exception, limit: int = 300) -> str:
    """El mensaje del error, recortado, para que una línea de log no se haga enorme."""
    text = str(error)
    return text if len(text) <= limit else text[:limit] + "..."
