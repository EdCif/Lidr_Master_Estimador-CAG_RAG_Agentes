"""Camino B · Extraer el texto en un proceso aparte, con tiempo máximo.

El problema que resuelve
------------------------
Los topes que ya tenemos miden TAMAÑOS: páginas de un PDF y Word
descomprimido (common.py), y contenido de las páginas descomprimido
(path_b_local.pdf_as_pypdf_sees_it). Pero un PDF trucado puede ser pequeño y
aun así costar muchísima CPU. Ejemplo: un "dibujo" interno A que dibuja
1.000 veces el dibujo B, que a su vez dibuja 1.000 veces el C. Ocupa unos
pocos KB, pasa todos los topes, y el lector acaba haciendo millones de
dibujos. Contra eso ningún tope de tamaño sirve: hace falta un límite de
TIEMPO.

Cómo funciona
-------------
1. La API lanza otro Python:
   ``python -m app.services.attachments.extraction_worker``.
2. Le pasa los adjuntos y los ajustes por su entrada estándar, en JSON (los
   bytes de cada fichero van en base64, que es una forma de escribir bytes
   como texto).
3. El proceso hijo extrae el texto con la función de siempre,
   ``path_b_local.prepare_user_content``, y devuelve el resultado también en
   JSON por su salida estándar.
4. Si tarda más de ``ATTACHMENT_EXTRACTION_TIMEOUT_S`` (30 s por defecto),
   ``subprocess.run`` mata el proceso hijo y respondemos con un error 413.
   Lo que se cuelga es el hijo; la API sigue atendiendo como si nada.

Por qué JSON y no pickle
------------------------
pickle es el formato propio de Python para mandar objetos, y sería más
cómodo, pero al leer un pickle se pueden ejecutar instrucciones escondidas
en él. El hijo es justo quien abre el fichero sospechoso: si algún día un
PDF lograra engañar al lector, con JSON lo peor que podría devolver son
datos raros, nunca código. Usamos JSON en los dos sentidos para no tener
que pensar en qué dirección es seguro y en cuál no.

Por qué subprocess y no multiprocessing
---------------------------------------
multiprocessing, en Windows, vuelve a cargar dentro del hijo el programa
principal (uvicorn, unittest...), con efectos raros. Y matar a un hijo
colgado es más enrevesado. ``subprocess.run(timeout=...)`` lo mata solo.

Lo que cuesta
-------------
Arrancar el proceso hijo lleva alrededor de un segundo (tiene que cargar
pypdf, python-docx...). Solo se paga en los turnos del Camino B que traen
adjuntos; sin adjuntos no se lanza nada.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
from pathlib import Path

from app.services.attachments import path_b_local
from app.services.attachments.common import Attachment, AttachmentError, PreparedContent

logger = logging.getLogger(__name__)

# La raíz del proyecto (estimador-cag): el hijo se lanza desde ahí para que
# pueda importar el paquete "app" igual que la API.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_WORKER_MODULE = "app.services.attachments.extraction_worker"


# ---------------------------------------------------------------------------
# Lado de la API: lanza el proceso hijo y espera como mucho timeout_s
# ---------------------------------------------------------------------------

def prepare_user_content_with_time_limit(
    transcript: str,
    attachments: list[Attachment],
    pdf_extractor: str,
    max_chars: int,
    timeout_s: float,
) -> PreparedContent:
    """Igual que ``path_b_local.prepare_user_content``, pero en otro proceso y con reloj.

    Es una función normal (no async): espera a que termine el hijo. El
    servicio la llama con asyncio.to_thread para que esa espera no frene al
    resto de peticiones.
    """
    if not attachments:
        # Sin adjuntos no hay nada que extraer: no merece la pena pagar el
        # segundo que cuesta arrancar otro Python.
        return path_b_local.prepare_user_content(transcript, [], pdf_extractor, max_chars)

    request = json.dumps({
        "transcript": transcript,
        "pdf_extractor": pdf_extractor,
        "max_chars": max_chars,
        "attachments": [
            {"filename": a.filename, "kind": a.kind, "data": base64.b64encode(a.data).decode("ascii")}
            for a in attachments
        ],
    }).encode("utf-8")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", _WORKER_MODULE],
            input=request,
            capture_output=True,
            timeout=timeout_s,
            cwd=_PROJECT_ROOT,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        # subprocess.run ya ha matado al hijo antes de lanzar esta excepción.
        raise AttachmentError(
            f"Extraer el texto de los adjuntos ha tardado más de {timeout_s:g} segundos "
            "y se ha cancelado. Puede ser un documento muy pesado o trucado: prueba con "
            "menos páginas o con el Camino A (attachment_mode=files_api).",
            status_code=413,
        ) from error

    try:
        result = json.loads(completed.stdout)
    except ValueError:
        # El hijo ha muerto sin contestar (un error nuestro, o sin memoria).
        # Los detalles van al log; al usuario, un mensaje claro.
        logger.error(
            "El proceso de extracción terminó sin respuesta (código %s): %s",
            completed.returncode,
            completed.stderr.decode("utf-8", errors="replace")[-2000:],
        )
        raise AttachmentError("No se ha podido extraer el texto de los adjuntos.") from None

    if "error" in result:
        # Un error previsto (PDF sin texto, demasiados caracteres...): lo
        # reconstruimos con su mismo mensaje y su mismo código HTTP.
        raise AttachmentError(result["error"]["message"], status_code=result["error"]["status_code"])
    return PreparedContent(
        user_content=result["user_content"],
        notes=result["notes"],
        attachment_chars=result["attachment_chars"],
    )


# ---------------------------------------------------------------------------
# Lado del proceso hijo: lo que se ejecuta con "python -m ..."
# ---------------------------------------------------------------------------

def _worker_main() -> None:
    """Lee la petición, extrae el texto y escribe la respuesta en JSON."""
    # Guardamos la salida estándar de verdad para la respuesta, y mandamos
    # cualquier print de las librerías a la salida de errores: así nada
    # puede colarse en medio del JSON.
    answer = sys.stdout.buffer
    sys.stdout = sys.stderr

    request = json.loads(sys.stdin.buffer.read())
    attachments = [
        Attachment(filename=item["filename"], kind=item["kind"], data=base64.b64decode(item["data"]))
        for item in request["attachments"]
    ]
    try:
        prepared = path_b_local.prepare_user_content(
            request["transcript"], attachments, request["pdf_extractor"], request["max_chars"]
        )
        result = {
            "user_content": prepared.user_content,
            "notes": prepared.notes,
            # Sesión 06: si no viaja en el JSON, se perdería por el camino.
            "attachment_chars": prepared.attachment_chars,
        }
    except AttachmentError as error:
        result = {"error": {"message": str(error), "status_code": error.status_code}}
    # Cualquier otra excepción deja la salida vacía y el código de salida
    # distinto de 0: la API lo trata como "no se ha podido extraer".
    answer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    answer.flush()


if __name__ == "__main__":
    _worker_main()
