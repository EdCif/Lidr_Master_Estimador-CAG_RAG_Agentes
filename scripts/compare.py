"""compare.py · Mide cuánto se parecen dos textos según el modelo de embeddings.

Le das dos textos, pide a OpenAI el vector de cada uno y calcula la similitud
coseno entre los dos vectores: un número que dice cuánto «apuntan en la misma
dirección», es decir, cuánto se parecen en significado.

    uv run python scripts/compare.py \\
        --text-a "OAuth 2.0 authentication backend for fintech" \\
        --text-b "JWT-based authorization service for banking app"

    Text A: OAuth 2.0 authentication backend for fintech
    Text B: JWT-based authorization service for banking app
    Cosine similarity: 0.8421

Cómo leer el número (de -1 a 1; en la práctica, de 0 a 1):

- Cerca de 1: dicen lo mismo, aunque con otras palabras.
- Cerca de 0,2-0,5: sin relación. OJO: no existe «el cero». Textos que no
  tienen nada que ver siguen dando 0,2-0,5, así que un 0,6 NO es «muy
  parecido». Cualquier umbral hay que calibrarlo con los datos propios.

Se puede lanzar de dos formas (el ``.env`` con la clave de OpenAI se lee solo):

- Fuera del contenedor:  ``uv run python scripts/compare.py --text-a "..." --text-b "..."``
- Dentro del contenedor: ``docker compose exec ai_service python scripts/compare.py --text-a "..." --text-b "..."``

Por defecto solo escribe las tres líneas de arriba. Con ``--verbose`` deja
también el registro de cada llamada (tokens y latencia).
"""

import argparse
import logging
import math
import sys
from pathlib import Path

# Este script vive en scripts/ y necesita importar el paquete ``app``: se añade
# la raíz del proyecto al camino de búsqueda, igual que hace validate_pipeline.py.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from openai import AuthenticationError, OpenAIError  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.embedding_pipeline.embedder import LOGGER_NAME as EMBEDDER_LOGGER  # noqa: E402
from app.embedding_pipeline.embedder import create_embedder  # noqa: E402


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Similitud coseno entre dos vectores, calculada a mano (sin librerías).

    Es el producto escalar dividido por el producto de las longitudes:

        coseno = (a · b) / (|a| · |b|)

    - ``a · b`` es la suma de multiplicar cada número de ``a`` por el de ``b``
      que está en la misma posición.
    - ``|a|`` (la norma) es la longitud del vector: la raíz de la suma de sus
      números al cuadrado.

    OpenAI devuelve los vectores ya con longitud 1, así que aquí el coseno
    coincide con el producto escalar. Se divide igualmente por las normas para
    que la función valga con cualquier vector.

    Lanza ``ValueError`` si los vectores miden distinto o si alguno es todo
    ceros (no tiene dirección y la división no tendría sentido).
    """
    if len(a) != len(b):
        raise ValueError(f"Los vectores deben medir lo mismo, pero miden {len(a)} y {len(b)}.")

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        raise ValueError("Un vector de todo ceros no tiene dirección: no se puede calcular el coseno.")
    return dot / (norm_a * norm_b)


def main(argv: list[str] | None = None, embedder=None) -> int:
    """Lee los dos textos, los vectoriza y escribe la similitud. Devuelve el código de salida.

    ``embedder`` se puede pasar desde fuera (los tests usan uno falso); si no,
    se crea el real con la clave del ``.env``.
    """
    parser = argparse.ArgumentParser(description="Similitud coseno entre dos textos (OpenAI embeddings).")
    parser.add_argument("--text-a", required=True, help="primer texto")
    parser.add_argument("--text-b", required=True, help="segundo texto")
    parser.add_argument("--verbose", action="store_true", help="deja también el registro de cada llamada")
    args = parser.parse_args(argv)

    for name, text in (("--text-a", args.text_a), ("--text-b", args.text_b)):
        if not text.strip():
            parser.error(f"{name} no puede estar vacío.")

    if not args.verbose:
        # El embedder deja una línea JSON por llamada; para una herramienta de
        # consola de tres líneas es ruido.
        logging.getLogger(EMBEDDER_LOGGER).setLevel(logging.WARNING)

    try:
        embedder = embedder or create_embedder()
        vector_a = embedder.embed_one(args.text_a)
        vector_b = embedder.embed_one(args.text_b)
        similarity = cosine_similarity(vector_a, vector_b)
    except ValidationError:
        print("Error: falta OPENAI_API_KEY. Defínela en el fichero .env.", file=sys.stderr)
        return 1
    except AuthenticationError:
        print("Error: OpenAI no acepta la clave. Revisa OPENAI_API_KEY en el fichero .env.", file=sys.stderr)
        return 1
    except OpenAIError as error:
        print(f"Error: no se ha podido llamar a OpenAI ({type(error).__name__}).", file=sys.stderr)
        return 1

    print(f"Text A: {args.text_a}")
    print(f"Text B: {args.text_b}")
    print(f"Cosine similarity: {similarity:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
