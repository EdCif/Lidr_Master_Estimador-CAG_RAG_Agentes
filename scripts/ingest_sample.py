"""ingest_sample.py · Manda el dataset de presupuestos al endpoint y guarda el resultado.

Hace de «cliente» del endpoint ``POST /embeddings/ingest``: lee
``data/budgets_sample.json`` (los 15 presupuestos de ejemplo), se lo manda a la
API y guarda lo que contesta (los chunks con sus vectores de 1.536 números y
las estadísticas) en ``data/embeddings_sample.json``, para poder abrirlo y
mirarlo con calma. Ese fichero no se sube a GitHub (está ignorado).

Necesita la API en marcha. Desde la raíz del repo, en otra terminal:

    uv run uvicorn app.main:app --host 127.0.0.1 --port 8001
    uv run python scripts/ingest_sample.py

Cuesta una fracción de céntimo (unos 6.000 tokens a 0,02 $ por millón).
"""

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "http://127.0.0.1:8001/embeddings/ingest"
DEFAULT_INPUT = PROJECT_ROOT / "data" / "budgets_sample.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "embeddings_sample.json"


def load_payload(path: Path) -> dict:
    """Lee la lista de presupuestos y la envuelve como espera el endpoint: ``{"budgets": [...]}``."""
    return {"budgets": json.loads(path.read_text(encoding="utf-8"))}


def summarize(result: dict, seconds: float) -> list[str]:
    """Las líneas del resumen: cuántos chunks, tokens, coste, tamaño de los vectores y tiempo."""
    stats = result["stats"]
    sizes = sorted({len(chunk["embedding"]) for chunk in result["chunks"]})
    by_sector = Counter(chunk["metadata"]["client_sector"] for chunk in result["chunks"])
    return [
        f"Presupuestos enviados:  {stats['total_budgets']}",
        f"Chunks recibidos:      {stats['total_chunks']}",
        f"Tokens totales:        {stats['total_tokens']}",
        f"Coste estimado:        {stats['estimated_cost_usd']:.6f} USD",
        f"Números por vector:    {', '.join(map(str, sizes))}",
        "Chunks por sector:     " + ", ".join(f"{sector} {count}" for sector, count in sorted(by_sector.items())),
        f"Tiempo:                {seconds:.1f} s",
    ]


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None) -> int:
    """Manda el dataset, guarda la respuesta y escribe el resumen. Devuelve el código de salida.

    ``client`` se puede pasar desde fuera (los tests usan uno que no sale a la
    red); si no, se crea uno normal.
    """
    parser = argparse.ArgumentParser(description="Manda el dataset de presupuestos a POST /embeddings/ingest.")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"dirección del endpoint (por defecto {DEFAULT_URL})")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="JSON con la lista de presupuestos")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="dónde guardar la respuesta")
    parser.add_argument("--timeout", type=float, default=120, help="segundos máximos de espera (por defecto 120)")
    args = parser.parse_args(argv)

    payload = load_payload(args.input)
    print(f"Enviando {len(payload['budgets'])} presupuestos a {args.url} ...")

    owns_client = client is None
    client = client or httpx.Client(timeout=args.timeout)
    started = time.perf_counter()
    try:
        response = client.post(args.url, json=payload)
    except httpx.ConnectError:
        print(
            f"Error: no encuentro la API en {args.url}. ¿Está arrancada?\n"
            "  uv run uvicorn app.main:app --host 127.0.0.1 --port 8001",
            file=sys.stderr,
        )
        return 1
    except httpx.TimeoutException:
        print(f"Error: la API no ha contestado en {args.timeout:g} segundos.", file=sys.stderr)
        return 1
    finally:
        if owns_client:
            client.close()
    seconds = time.perf_counter() - started

    if response.status_code != 200:
        print(f"Error: la API contestó {response.status_code}.", file=sys.stderr)
        print(f"  {response.text[:500]}", file=sys.stderr)
        return 1

    result = response.json()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    print("\n".join(summarize(result, seconds)))
    try:
        shown = args.output.relative_to(PROJECT_ROOT)
    except ValueError:
        shown = args.output
    print(f"Guardado en:           {shown} ({args.output.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
