"""Piezas compartidas por los tests de integración con pytest (Paso 03).

pytest lee este fichero solo, antes de los tests. Aquí se definen los
"fixtures": piezas preparadas que un test pide con solo nombrarlas como
parámetro. Por ejemplo, ``async def test_algo(client, fake_llm)`` recibe un
cliente HTTP listo para usar y el modelo falso, sin montarlos a mano.

Los tests de integración recorren la API de verdad (formulario multipart,
validaciones, extracción de los adjuntos, plantilla Jinja2 del system prompt,
extractor de memoria, ventana del historial...). Lo único falso es el modelo:
llamar a Claude cuesta dinero, tarda y no responde siempre lo mismo, así que
lo sustituimos por ``FakeLLM``, que contesta al instante y de forma previsible.
"""

import copy

import httpx
import pytest
from fastapi import FastAPI

from api_helpers import fake_settings
from app.config import get_settings
from app.errors import install_error_handlers
from app.routers.sessions import get_session_store, router
from app.services import conversation_service
from app.services.prompt_builder import build_session_system_prompt
from app.services.sessions import SessionStore

# Tecnologías que el modelo falso "reconoce" en lo que le llega.
_KNOWN_TECHNOLOGIES = ["React", "FastAPI", "PostgreSQL", "Kubernetes", "Kafka"]


class FakeLLM:
    """Modelo de mentira que imita lo justo a Claude.

    * Guarda una copia de los mensajes de cada llamada (``calls``), para que
      los tests comprueben qué le habría llegado al modelo de verdad.
    * Responde según lo que ve en el último mensaje del usuario: más horas si
      aparece "Kubernetes" (que es lo que pondremos en el PDF de prueba), y
      cierra con el bloque <hechos_del_proyecto>, como pide la plantilla.
    """

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def stream_reply(self, messages, *, model, api_key, max_tokens, stats):
        self.calls.append(copy.deepcopy(messages))
        question = messages[-1]["content"]
        technologies = [tech for tech in _KNOWN_TECHNOLOGIES if tech in question]
        hours = 300 + (120 if "Kubernetes" in question else 0)

        reply = (
            f"Estimación: {hours} horas.\n"
            "<hechos_del_proyecto>\n"
            "Proyecto: sin definir\n"
            "Equipo asumido: sin definir\n"
            f"Tecnologías: {', '.join(technologies) or 'sin definir'}\n"
            "Alcance acordado: sin definir\n"
            "</hechos_del_proyecto>"
        )
        # Lo entregamos en dos trozos, como llegaría en streaming.
        for chunk in (reply[:20], reply[20:]):
            yield chunk
        stats.final_text = reply
        stats.input_tokens, stats.output_tokens = 1000, 100


@pytest.fixture
def fake_llm(monkeypatch):
    """El modelo falso, colocado en lugar del de verdad (Camino B, litellm)."""
    llm = FakeLLM()
    monkeypatch.setattr(conversation_service.path_b_local, "stream_reply", llm.stream_reply)
    return llm


@pytest.fixture
def store():
    """Un almacén de sesiones nuevo para cada test, con la plantilla Jinja2 real."""
    return SessionStore(system_prompt_builder=build_session_system_prompt, max_turns=6)


@pytest.fixture
async def client(store, fake_llm):
    """Cliente HTTP asíncrono (httpx.AsyncClient) contra la API, sin red.

    ASGITransport hace que las peticiones vayan directamente a la aplicación
    FastAPI en memoria, sin abrir ningún puerto. El "base_url" es inventado.
    """
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_session_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: fake_settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
