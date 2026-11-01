"""El embedder: convierte los chunks en vectores llamando a OpenAI.

Un *embedding* (vector) es una lista de números que representa el significado
de un texto: textos que dicen lo mismo con palabras distintas dan listas
parecidas. Con ``text-embedding-3-small`` cada texto da 1.536 números.

Qué hace este fichero, y con qué cuidado:

- **Lotes.** Manda hasta 100 textos en cada llamada (``BATCH_SIZE``) en vez de
  una llamada por chunk: es más rápido y no martillea la API con peticiones
  seguidas.
- **Reintentos solo para el «espera un poco» (error 429).** Un 429 se arregla
  esperando; una clave mala o una petición inválida, no. Se reintenta tres
  veces, esperando 1, 2 y 4 segundos. Cualquier otro error sube tal cual.
- **Fallar duro.** Si OpenAI devuelve menos vectores de los pedidos, o con otra
  longitud, se lanza un error: preferimos parar a entregar datos a medias.
- **Cuentas a la vista.** Cada lote deja una línea JSON ``embedding_batch`` con
  los chunks, los tokens y la latencia; y el coste se calcula con el precio
  como constante (``PRICE_USD_PER_MILLION_TOKENS``), porque el precio cambia.

La llamada clave, que es lo único que sale del ordenador::

    client.embeddings.create(model="text-embedding-3-small", input=[texto1, texto2, ...])
        -> response.data[i].embedding   # 1536 decimales del texto i (mismo orden)
        -> response.usage.total_tokens  # tokens que factura OpenAI
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from openai import OpenAI, RateLimitError

from app.config import get_settings
from app.embedding_pipeline.chunker import TokenCounter, count_tokens_with_tiktoken
from app.embedding_pipeline.schemas import Chunk, EmbeddedChunk
from app.services.json_logger import make_json_logger

# Nombre del logger: así se buscan sus líneas en la consola y en los tests.
LOGGER_NAME = "embedding_embedder"
_log = make_json_logger(LOGGER_NAME)

# Modelo por defecto. En la aplicación real lo decide EMBEDDING_MODEL (.env).
EMBEDDING_MODEL = "text-embedding-3-small"

# Cuántos números tiene el vector de ese modelo. No se pide una dimensión
# menor (eso es el truco «Matryoshka», tema del directo). Si algún día se
# cambia de modelo, hay que cambiar esto y el tamaño de la columna de la
# base de datos de la sesión 8.
EMBEDDING_DIMENSIONS = 1536

# Textos por llamada. OpenAI admite hasta 2.048; 100 es la recomendación del
# enunciado para no hacer llamadas enormes.
BATCH_SIZE = 100

# Esperas, en segundos, antes de cada reintento ante un 429 (3 reintentos).
RETRY_DELAYS_S = (1, 2, 4)

# PRECIO, en dólares por millón de tokens de entrada, de text-embedding-3-small.
# Es el que da el enunciado de la sesión 7 (consultado en 2026/11). El precio
# cambia con el tiempo: si cambia, se corrige solo aquí.
PRICE_USD_PER_MILLION_TOKENS = 0.02


def estimate_cost_usd(tokens: int) -> float:
    """Coste estimado, en dólares, de vectorizar ``tokens`` tokens."""
    return tokens / 1_000_000 * PRICE_USD_PER_MILLION_TOKENS


class EmbeddingResponseError(RuntimeError):
    """OpenAI contestó, pero no con lo que se pidió (faltan vectores o miden otra cosa)."""


@dataclass
class _BatchResult:
    """Lo que devuelve una llamada por lotes, con lo necesario para el log."""

    vectors: list[list[float]]
    api_tokens: int  # los tokens que factura OpenAI (response.usage.total_tokens)
    retries: int  # cuántos 429 hubo que esperar antes de acertar
    latency_ms: int  # lo que tardó el lote entero, esperas incluidas


class OpenAIEmbedder:
    """Convierte textos y chunks en vectores. Métodos públicos: ``embed_one`` y ``embed_many``.

    El cliente de OpenAI se recibe desde fuera (así los tests le pasan uno
    falso). ``sleep`` y ``count_tokens`` también se pueden sustituir: los tests
    no esperan de verdad ni descargan tiktoken.
    """

    def __init__(
        self,
        client: OpenAI,
        *,
        model: str = EMBEDDING_MODEL,
        sleep: Callable[[float], None] = time.sleep,
        count_tokens: TokenCounter | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._sleep = sleep
        self._count_tokens = count_tokens or count_tokens_with_tiktoken

    def embed_one(self, text: str) -> list[float]:
        """El vector de un solo texto. Lo usa ``scripts/compare.py``."""
        result = self._embed_texts([text])
        self._log_batch(batch=1, chunks=1, tokens=self._count_tokens(text), result=result)
        return result.vectors[0]

    def embed_many(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        """Un ``EmbeddedChunk`` por cada chunk, en el mismo orden.

        Corta la lista en lotes de ``BATCH_SIZE``, manda cada lote a OpenAI y
        casa cada vector con su chunk por posición (OpenAI los devuelve en el
        orden en que se mandaron los textos).
        """
        embedded: list[EmbeddedChunk] = []
        for batch_number, start in enumerate(range(0, len(chunks), BATCH_SIZE), start=1):
            batch = chunks[start : start + BATCH_SIZE]
            result = self._embed_texts([chunk.text for chunk in batch])
            self._log_batch(
                batch=batch_number,
                chunks=len(batch),
                # Los tokens que ya contó el chunker con tiktoken; no se recuentan.
                tokens=sum(chunk.token_count for chunk in batch),
                result=result,
            )
            embedded.extend(
                EmbeddedChunk(**chunk.model_dump(), embedding=vector)
                for chunk, vector in zip(batch, result.vectors, strict=True)
            )
        return embedded

    def _embed_texts(self, texts: list[str]) -> _BatchResult:
        """Una llamada a OpenAI con estos textos; comprueba que la respuesta cuadra."""
        started = time.perf_counter()
        response, retries = self._create_with_retry(texts)
        latency_ms = round((time.perf_counter() - started) * 1000)

        # Por si acaso, se ordenan por «index» (la posición del texto que se
        # mandó): así el vector i es siempre el del texto i.
        items = sorted(response.data, key=lambda item: item.index)
        vectors = [list(item.embedding) for item in items]

        if len(vectors) != len(texts):
            raise EmbeddingResponseError(
                f"Se pidieron {len(texts)} vectores y OpenAI devolvió {len(vectors)}."
            )
        for position, vector in enumerate(vectors):
            if len(vector) != EMBEDDING_DIMENSIONS:
                raise EmbeddingResponseError(
                    f"El vector {position} tiene {len(vector)} números y se esperaban {EMBEDDING_DIMENSIONS}."
                )
        return _BatchResult(
            vectors=vectors,
            api_tokens=response.usage.total_tokens,
            retries=retries,
            latency_ms=latency_ms,
        )

    def _create_with_retry(self, texts: list[str]):
        """Llama a OpenAI; ante un 429 («demasiadas peticiones») espera y reintenta.

        Solo el 429 se reintenta: es el único error que se arregla esperando.
        Una clave incorrecta (401), una petición inválida (400) o una caída de
        OpenAI (500) no mejoran por esperar, y reintentarlos escondería el
        fallo real. Esos suben hacia arriba tal cual.

        El cliente se crea con ``max_retries=0`` (ver ``create_embedder``): el
        SDK de OpenAI ya reintenta por su cuenta, y dejarlo multiplicaría las
        esperas sin que lo viéramos. Así los únicos reintentos son estos.

        Devuelve la respuesta y cuántos reintentos hizo falta.
        """
        retries = 0
        # Los intentos son 1 + los reintentos; el ``None`` final marca «ya no
        # quedan esperas»: si falla otra vez, el error sube.
        for delay in (*RETRY_DELAYS_S, None):
            try:
                response = self._client.embeddings.create(model=self._model, input=texts)
                return response, retries
            except RateLimitError:
                if delay is None:
                    raise
                self._sleep(delay)
                retries += 1
        raise AssertionError("inalcanzable: el último intento devuelve o lanza")  # pragma: no cover

    def _log_batch(self, *, batch: int, chunks: int, tokens: int, result: _BatchResult) -> None:
        """Una línea JSON por lote: cuántos chunks, cuántos tokens y cuánto tardó."""
        _log.info(
            "embedding_batch",
            batch=batch,
            chunks=chunks,
            tokens=tokens,  # los contados por tiktoken (lo que esperamos pagar)
            api_tokens=result.api_tokens,  # los que dice OpenAI que facturó
            latency_ms=result.latency_ms,
            retries=result.retries,
            model=self._model,
        )


def create_embedder() -> OpenAIEmbedder:
    """Monta un embedder real con la clave y el modelo del ``.env``.

    Es la misma cadena de la sesión 1: ``.env`` → ``get_settings()`` → clave →
    OpenAI. Lo usan el endpoint y ``scripts/compare.py``.

    Se usa el cliente síncrono (``OpenAI``, no ``AsyncOpenAI``) porque así lo
    pide la firma del enunciado, y ``max_retries=0`` para que los únicos
    reintentos sean los de ``_create_with_retry``.
    """
    settings = get_settings()
    client = OpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=60.0,
        max_retries=0,
    )
    return OpenAIEmbedder(client, model=settings.embedding_model)
