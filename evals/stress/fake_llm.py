"""Un modelo de mentira para probar el stress test sin gastar dinero.

Para qué sirve
--------------
El runner (``run.py``) con ``--fake-llm`` pone este modelo en lugar de
Claude. Así se puede comprobar que toda la cadena funciona (sesiones,
adjuntos, observación del turno, métricas, CSV, informe) en segundos, sin
clave ni red. Lo usan también los tests.

Se comporta como un modelo "obediente" y previsible:

* Respeta la memoria: copia en su bloque ``<hechos_del_proyecto>`` lo que ve
  en el bloque ``<project_metadata>`` del system prompt, igual que pide la
  plantilla. (Por eso, tras el pivote, arrastra React: lo tiene en la
  memoria. Es el comportamiento que queremos poder medir.)
* Usa el documento solo mientras lo tiene delante: menciona los datos
  plantados del PDF únicamente si aparecen en el ÚLTIMO mensaje del cliente.
* Cuenta los tokens de forma aproximada (4 caracteres ≈ 1 token) y tarda un
  poco más cuanto más le mandan, para que las curvas no salgan planas.

No es una simulación fiel de Claude: es un sustituto para probar la
fontanería. Las cifras de verdad salen de la batería con el modelo real.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from evals.stress.fixtures.build_pdfs import PLANTED_FACTS

_METADATA_BLOCK = re.compile(r"<project_metadata>(.*?)</project_metadata>", re.S)


def _text(content: Any) -> str:
    """El texto de un mensaje, sea un texto o una lista de bloques."""
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    return str(content or "")


class FakeStressLLM:
    """Sustituto de ``path_b_local.stream_reply`` con la misma firma."""

    def __init__(self, seconds_per_million_tokens: float = 2.0) -> None:
        # 2 s por millón de tokens: un PDF de 100 KB (~25.000 tokens) añade
        # unos 50 ms. Suficiente para que la latencia crezca con el tamaño.
        self.seconds_per_million_tokens = seconds_per_million_tokens
        self.calls = 0

    async def stream_reply(self, messages, *, model, api_key, max_tokens, stats):
        self.calls += 1
        system_prompt = _text(messages[0]["content"])
        question = " ".join(_text(messages[-1]["content"]).split())  # sin saltos de línea
        user_turns = sum(1 for message in messages if message["role"] == "user")

        # Lo que dice el documento, solo si está en el mensaje de este turno.
        seen = [
            fact.fact for fact in PLANTED_FACTS
            if re.search(rf"(?<!\w){re.escape(fact.fact)}(?!\w)", question, re.IGNORECASE)
        ]
        memory = _METADATA_BLOCK.search(system_prompt)
        memory_lines = memory.group(1).strip() if memory else ""

        reply = f"Estimación del modelo de prueba: {200 + 10 * user_turns} horas.\n"
        if seen:
            reply += "Requisitos del documento tenidos en cuenta: " + ", ".join(seen) + ".\n"
        reply += f"<hechos_del_proyecto>\n{memory_lines or 'Proyecto: sin definir'}\n</hechos_del_proyecto>"

        input_tokens = sum(len(_text(message["content"])) for message in messages) // 4
        await asyncio.sleep(input_tokens * self.seconds_per_million_tokens / 1_000_000)

        # En dos trozos, como llegaría en streaming.
        middle = len(reply) // 2
        for chunk in (reply[:middle], reply[middle:]):
            yield chunk
        stats.final_text = reply
        stats.input_tokens = input_tokens
        stats.output_tokens = len(reply) // 4
