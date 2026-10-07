"""Endpoints HTTP de las sesiones conversacionales (sesión 05).

Igual que el resto de routers, esta capa solo recibe la petición, delega y
devuelve. La lógica del historial y de la memoria está en
``app/services/sessions.py``.

Las rutas cuelgan de /sessions, sin el prefijo /api/v1 que usan las de las
sesiones anteriores, porque así las define el enunciado de la sesión 05. Los
endpoints antiguos no se tocan y siguen funcionando igual.
"""

from functools import lru_cache
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.schemas.session import SessionCreated, SessionState
from app.services.llm_service import build_system_prompt
from app.services.sessions import (
    ProjectMetadata,
    Session,
    SessionNotFoundError,
    SessionStore,
)


router = APIRouter(prefix="/sessions", tags=["sessions"])


# ---------------------------------------------------------------------------
# El almacén de sesiones (uno para todo el proceso)
# ---------------------------------------------------------------------------

def provisional_prompt_builder(metadata: ProjectMetadata) -> str:
    """System prompt PROVISIONAL, solo hasta el Paso 4.

    De momento devolvemos el system prompt CAG de la sesión 1 tal cual e
    ignoramos la ficha del proyecto. En el Paso 4 lo cambiaremos por una
    plantilla Jinja2 que sí meta la ficha en un bloque <project_metadata>.
    Como el almacén recibe esta función desde fuera, ese cambio será tocar
    una sola línea en get_session_store().
    """
    return build_system_prompt()


@lru_cache(maxsize=1)
def get_session_store() -> SessionStore:
    """Devuelve el almacén de sesiones del proceso, siempre el mismo objeto.

    lru_cache(maxsize=1) hace que el almacén se cree la primera vez que se
    pide y que las siguientes llamadas devuelvan ese mismo objeto. Es la misma
    técnica que usa get_settings() en config.py.

    Los endpoints lo reciben con Depends(get_session_store) en vez de
    importarlo directamente. Así, en los tests podemos sustituirlo por un
    almacén vacío con app.dependency_overrides y cada prueba empieza limpia.
    """
    return SessionStore(system_prompt_builder=provisional_prompt_builder)


def get_session_or_404(session_id: UUID, store: SessionStore) -> Session:
    """Busca la sesión y, si no existe, responde 404 con una explicación útil.

    Declarar session_id como UUID hace que FastAPI rechace con un 422 los
    identificadores mal formados antes de llegar aquí. str(session_id) lo
    deja en el formato en que lo guardamos (minúsculas y con guiones), aunque
    el cliente lo mande en mayúsculas.
    """
    try:
        return store.get(str(session_id))
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "La sesión no existe o ha caducado (el servidor se ha reiniciado "
                "o se ha superado el límite de sesiones). Crea una nueva con POST /sessions."
            ),
        ) from None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", response_model=SessionCreated, status_code=status.HTTP_201_CREATED)
async def create_session(store: SessionStore = Depends(get_session_store)):
    """Crea una sesión vacía y devuelve su session_id.

    Respondemos 201 (Created) en lugar de 200 porque la petición crea algo
    nuevo en el servidor. La sesión nace sin historial y con la ficha del
    proyecto vacía; se irá llenando con cada llamada a /estimate.
    """
    session = store.create()
    return SessionCreated(session_id=session.session_id)


@router.get("/{session_id}", response_model=SessionState)
async def get_session(session_id: UUID, store: SessionStore = Depends(get_session_store)):
    """Devuelve la memoria y el historial actuales de una sesión.

    Es de solo lectura: consultar una sesión no cambia ni su historial ni su
    ficha (solo la marca como usada, para que no sea la primera en borrarse).
    """
    session = get_session_or_404(session_id, store)
    return SessionState(
        session_id=session.session_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        max_turns=session.history.max_turns,
        turn_count=session.history.turn_count,
        discarded_turns=session.history.discarded_turns,
        project_metadata=session.metadata,
        history=session.history.messages,
    )
