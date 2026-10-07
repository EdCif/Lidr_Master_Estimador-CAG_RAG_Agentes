"""Valida estructura, pruebas y el flujo HTTP -> contexto CAG -> SDK -> respuesta.

Por defecto usa una clave ficticia y un transporte HTTP simulado, sin conexión
con OpenAI. --live realiza una sola petición real con la configuración del proyecto.
Los informes guardan metadatos, nunca la clave ni la transcripción completa.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

REQUIRED_FILES = (
    "app/__init__.py",
    "app/main.py",
    "app/config.py",
    "app/errors.py",
    "app/routers/__init__.py",
    "app/routers/estimations.py",
    "app/routers/ideas.py",
    "app/routers/sessions.py",
    "app/services/__init__.py",
    "app/services/llm_service.py",
    "app/services/idea_service.py",
    "app/services/sessions.py",
    "app/schemas/__init__.py",
    "app/schemas/estimation.py",
    "app/schemas/idea.py",
    "app/schemas/session.py",
    "app/context/__init__.py",
    "app/context/examples.py",
    "app/web/index.html",
    "app/web/app.js",
    "app/web/styles.css",
    "tests/test_estimations.py",
    "tests/test_ideas.py",
    "tests/test_llm_service.py",
    "tests/test_sessions.py",
    "tests/test_sessions_api.py",
    "docs/transcripcion_reunion.md",
    "README.md",
    ".env.example",
    ".gitignore",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/validate.yml",
)

OFFLINE_ENV = {
    "OPENAI_API_KEY": "pipeline-fake-key-no-real-credential",
    "LLM_MODEL": "pipeline-test-model",
    "OPENAI_MODEL": "pipeline-test-model",
    "LLM_PROVIDER": "openai",
    "APP_ENV": "test",
    "APP_DEBUG": "false",
    "LOG_LEVEL": "WARNING",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONDONTWRITEBYTECODE": "1",
}

SIMULATED_ESTIMATION = """# Estimación simulada para validar el flujo

Esta respuesta es una muestra técnica fija, sin generación de un modelo.

## Alcance
Módulo de disponibilidad, reservas, cambios, cancelaciones y confirmación por correo.

## Supuestos y exclusiones
Se reutilizan autenticación, infraestructura y proveedor de correo.
Quedan fuera pagos, aplicaciones móviles nativas y calendarios externos.

## Desglose de referencia
| Partida | Horas |
| --- | ---: |
| Análisis y diseño | 12 |
| Backend y concurrencia | 22 |
| Interfaz de usuario | 16 |
| Notificaciones | 6 |
| Pruebas y documentación | 20 |
| Contingencia | 12 |
| Total | 88 |

## Riesgos y equipo
Pendiente acordar cierres y conservación del historial. Revisar con desarrollo y producto.
Las horas son esfuerzo de una persona; no equivalen a un plazo de calendario.
"""


class PipelineError(Exception):
    """Error previsto con un mensaje seguro para consola e informe."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def check_structure() -> dict:
    missing = [name for name in REQUIRED_FILES if not (PROJECT_ROOT / name).is_file()]
    require(not missing, "Faltan archivos de la estructura: " + ", ".join(missing))
    python_files = sorted((PROJECT_ROOT / "app").rglob("*.py"))
    python_files += sorted((PROJECT_ROOT / "tests").rglob("*.py"))
    python_files += sorted((PROJECT_ROOT / "scripts").rglob("*.py"))
    require(bool(list((PROJECT_ROOT / "tests").glob("test_*.py"))), "No hay pruebas test_*.py.")
    for path in python_files:
        try:
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except SyntaxError as error:
            raise PipelineError(
                f"Error de sintaxis en {path.relative_to(PROJECT_ROOT)}:{error.lineno}."
            ) from None
    return {"required_files": len(REQUIRED_FILES), "python_files_parsed": len(python_files)}


def run_tests() -> dict:
    # Otro proceso aísla los parches de tests de la prueba completa de la API.
    with tempfile.TemporaryDirectory(prefix="estimador-tests-") as data_dir:
        environment = {**os.environ, **OFFLINE_ENV, "DATA_DIR": data_dir}
        result = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    # Las pruebas usan datos ficticios; la salida ayuda a localizar una regresión.
    output = result.stdout + result.stderr
    print(output.rstrip())
    match = re.search(r"Ran (\d+) tests?", output)
    count = int(match.group(1)) if match else 0
    require(count > 0, "La ejecución no ha confirmado ninguna prueba.")
    require(result.returncode == 0, "La suite de pruebas ha fallado; revisa su salida.")
    return {"tests_run": count, "exit_code": result.returncode}


def check_flow(transcription: str, live: bool, report: dict) -> str:
    import httpx
    from fastapi.testclient import TestClient
    from openai import AsyncOpenAI

    from app.config import Settings, get_settings
    from app.context.examples import ESTIMATION_EXAMPLES
    from app.services import llm_service

    # Evita que una variable de logging externa imprima cuerpos HTTP del SDK.
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    flow: dict = {"mode": "live" if live else "simulated", "sdk_requests": 0}
    report["flow"] = flow

    with tempfile.TemporaryDirectory(prefix="estimador-flow-") as data_dir, ExitStack() as stack:
        environment = {"DATA_DIR": data_dir} if live else {**OFFLINE_ENV, "DATA_DIR": data_dir}
        stack.enter_context(patch.dict(os.environ, environment))
        if not live:
            # El modo simulado no necesita ni carga el archivo .env del usuario.
            stack.enter_context(patch.dict(Settings.model_config, {"env_file": None}))
        get_settings.cache_clear()
        stack.callback(get_settings.cache_clear)
        settings = get_settings()
        reference_text = llm_service.format_examples(ESTIMATION_EXAMPLES)
        require(len(ESTIMATION_EXAMPLES) >= 2, "Se necesitan al menos dos referencias CAG.")

        async def inspect_request(request: httpx.Request) -> None:
            payload = json.loads(request.content)
            flow["sdk_requests"] += 1
            require(flow["sdk_requests"] == 1, "El flujo intentó más de una llamada al proveedor.")
            require(request.url.path == "/v1/chat/completions", "Ruta inesperada del proveedor.")
            require(request.method == "POST", "El SDK no envió un POST.")
            messages = payload.get("messages", [])
            require([m.get("role") for m in messages] == ["system", "user"], "Roles incorrectos.")
            require(reference_text in messages[0]["content"], "Faltan referencias CAG en system.")
            require(len(messages[0]["content"]) > len(reference_text), "Faltan instrucciones system.")
            require(messages[1]["content"] == transcription, "La transcripción llegó modificada.")
            require(payload.get("model") == settings.llm_model, "No se usa el modelo configurado.")
            flow.update({
                "reference_count": len(ESTIMATION_EXAMPLES),
                "reference_context_verified": True,
                "transcription_verified": True,
                "message_roles": ["system", "user"],
            })

        def simulate_response(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "id": "chatcmpl-pipeline-simulated",
                "object": "chat.completion",
                "created": 0,
                "model": settings.llm_model,
                "choices": [{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": SIMULATED_ESTIMATION},
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        def client_factory(**kwargs):
            # Se ejecuta el SDK real. Solo su transporte se sustituye en modo offline.
            http_options = {"event_hooks": {"request": [inspect_request]}}
            if not live:
                http_options.update(transport=httpx.MockTransport(simulate_response), trust_env=False)
            kwargs.update(
                http_client=httpx.AsyncClient(**http_options),
                max_retries=0,
                base_url="https://api.openai.com/v1",
            )
            return AsyncOpenAI(**kwargs)

        stack.enter_context(patch.object(llm_service, "AsyncOpenAI", side_effect=client_factory))
        # La configuración temporal está preparada antes de importar la aplicación.
        from app.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            health = client.get("/health")
            require(health.status_code == 200 and health.json().get("status") == "ok", "Healthcheck fallido.")
            home = client.get("/")
            require(home.status_code == 200 and "text/html" in home.headers.get("content-type", ""), "La web no responde.")
            for asset in ("app.js", "styles.css"):
                response = client.get(f"/assets/{asset}")
                require(response.status_code == 200 and bool(response.content), "Falta un recurso de la web.")
            invalid = client.post("/api/v1/estimate", json={"transcription": "  "})
            require(invalid.status_code == 422, "La API no rechaza transcripciones vacías.")
            require(flow["sdk_requests"] == 0, "Una transcripción vacía llegó al proveedor.")
            started = time.monotonic()
            response = client.post("/api/v1/estimate", json={"transcription": transcription})
            flow.update(http_status=response.status_code, elapsed_seconds=round(time.monotonic() - started, 3))
            require(response.status_code == 200, f"La estimación devolvió HTTP {response.status_code}; comprueba configuración y servicio.")
            result = response.json()
            for key in ("estimation", "model", "provider"):
                require(isinstance(result.get(key), str) and bool(result[key].strip()), f"Respuesta inválida: {key}.")
            require(flow["sdk_requests"] == 1, "La API no recorrió el SDK de OpenAI.")
            require(result["model"] == settings.llm_model, "El modelo devuelto no coincide con la configuración.")
            require(result["provider"] == "openai", "Proveedor inesperado en la respuesta.")
            if not live:
                require(result["estimation"] == SIMULATED_ESTIMATION.strip(), "No se devolvió la respuesta del SDK.")
            flow.update(model=result["model"], provider=result["provider"], estimation_characters=len(result["estimation"]))
            return result["estimation"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Realiza una llamada real a OpenAI (consume API).")
    parser.add_argument("--transcription", type=Path, default=PROJECT_ROOT / "docs/transcripcion_reunion.md")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "artifacts")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    mode = "live" if args.live else "simulated"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    report_path = args.output_dir / f"pipeline-{mode}-{stamp}.json"
    estimation_path = args.output_dir / f"estimation-{mode}-{stamp}.md"
    report = {"status": "failed", "mode": mode, "created_at": stamp, "checks": {}}
    stage = "structure"
    try:
        print("[1/4] Validando estructura y sintaxis de Python...")
        report["checks"][stage] = check_structure()
        stage = "transcription"
        print("[2/4] Cargando la transcripción del ejercicio...")
        transcription = args.transcription.read_text(encoding="utf-8-sig").strip()
        require(bool(transcription), "El archivo de transcripción está vacío.")
        report["checks"][stage] = {
            "filename": args.transcription.name,
            "characters": len(transcription),
            "sha256": hashlib.sha256(transcription.encode("utf-8")).hexdigest(),
        }
        stage = "tests"
        print("[3/4] Ejecutando todas las pruebas con datos ficticios...")
        report["checks"][stage] = run_tests()
        stage = "flow"
        print(f"[4/4] Validando flujo completo ({mode})...")
        estimation = check_flow(transcription, args.live, report)
        estimation_path.write_text(estimation + "\n", encoding="utf-8")
        report.update(status="passed", estimation_file=estimation_path.name)
    except Exception as error:
        # No serializar excepciones de SDK/Pydantic: podrían contener datos privados.
        message = str(error) if isinstance(error, PipelineError) else "Error en la etapa; revisa dependencias, archivo de entrada y configuración."
        report["error"] = {"stage": stage, "type": type(error).__name__, "message": message}
        print(f"ERROR [{stage}]: {message}", file=sys.stderr)
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Informe: {report_path}")
    if report["status"] == "passed":
        print(f"OK: pipeline completo. Estimación: {estimation_path}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
