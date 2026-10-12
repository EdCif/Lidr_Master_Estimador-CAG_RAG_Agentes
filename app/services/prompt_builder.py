"""System prompt de las sesiones (sesión 05): plantilla Jinja2 + memoria del proyecto.

Qué hace
--------
Rellena la plantilla ``app/prompts/estimator_system_v1.j2`` con dos cosas:

* Los ejemplos de referencia CAG de la sesión 1 (``app/context/examples.py``).
* La ficha del proyecto (``ProjectMetadata``), dentro del bloque
  ``<project_metadata>``. Es la MEMORIA de la conversación: así el modelo
  sigue sabiendo cómo se llama el proyecto o qué equipo se asumió aunque el
  turno en que se dijo ya haya salido de la ventana del historial.

Si la ficha está vacía (primer turno de la sesión), el bloque aparece vacío.

Quién lo usa
------------
El almacén de sesiones recibe ``build_session_system_prompt`` al crearse
(``get_session_store()`` en ``app/routers/sessions.py``). Cada vez que el
historial monta los mensajes para el modelo (``to_messages_list()``), llama
a esta función con la ficha de ese momento. Por eso el system prompt sale
siempre al día.

¿Y el prompt de la sesión 1?
----------------------------
``llm_service.build_system_prompt()`` no se toca: lo usan el endpoint
``/api/v1/estimate`` y Streamlit de la sesión 2, y la CI comprueba su forma.
Aceptamos que las instrucciones estén repetidas en los dos sitios a cambio de
no romper nada de lo anterior.

Un cuidado de seguridad
-----------------------
Lo que va en la ficha sale de lo que escribe el cliente (lo extrae el Paso
01.3). Como acaba dentro del system prompt, alguien podría intentar colar
ahí una instrucción, o cerrar el bloque ``</project_metadata>`` para salirse
de él. El filtro ``as_fact`` deja cada hecho en una sola línea corta y sin
los símbolos < y >, y la plantilla le dice al modelo que son datos, no
instrucciones.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.context.examples import ESTIMATION_EXAMPLES
from app.services.llm_service import format_examples
from app.services.sessions import ProjectMetadata

# Carpeta de las plantillas y versión en uso. Para estrenar una versión nueva
# del prompt se crea estimator_system_v2.j2 y se cambia solo esta línea.
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
SYSTEM_PROMPT_TEMPLATE = "estimator_system_v1.j2"

# Largo máximo de un hecho de la ficha dentro del prompt. Un nombre de
# proyecto o un alcance pactado caben de sobra; un párrafo entero, no.
MAX_FACT_LENGTH = 200


def as_fact(value: object) -> str:
    """Prepara un valor de la ficha para meterlo en el prompt como dato.

    * Todo en una línea: los saltos de línea y espacios repetidos se juntan.
    * Sin < ni >: así nadie puede cerrar el bloque <project_metadata> ni
      abrir otro inventado desde un dato.
    * Recortado a MAX_FACT_LENGTH caracteres.
    """
    text = re.sub(r"[<>]", "", str(value))
    text = " ".join(text.split())
    if len(text) > MAX_FACT_LENGTH:
        text = text[: MAX_FACT_LENGTH - 1].rstrip() + "…"
    return text


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """El "motor" de Jinja2, creado una sola vez.

    * FileSystemLoader: busca las plantillas en app/prompts.
    * autoescape=False: esto no es HTML, es texto para el modelo.
    * trim_blocks / lstrip_blocks: las líneas con solo un {% if %} no dejan
      líneas en blanco sueltas en el resultado.
    * StrictUndefined: si la plantilla usa una variable que no le pasamos,
      falla en vez de dejar el hueco vacío sin avisar.
    """
    environment = Environment(
        loader=FileSystemLoader(PROMPTS_DIR),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    environment.filters["as_fact"] = as_fact
    return environment


def build_session_system_prompt(metadata: ProjectMetadata) -> str:
    """Devuelve el system prompt con los ejemplos CAG y la ficha actual del proyecto."""
    template = _environment().get_template(SYSTEM_PROMPT_TEMPLATE)
    return template.render(
        examples=format_examples(ESTIMATION_EXAMPLES),
        metadata=metadata,
    )
