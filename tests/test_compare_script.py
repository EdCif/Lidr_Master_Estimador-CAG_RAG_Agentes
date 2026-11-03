"""Pruebas de scripts/compare.py (sesión 7): el coseno hecho a mano y la línea de comandos.

El coseno se prueba con vectores «de libro de texto» (iguales, perpendiculares,
opuestos...), de los que se sabe la respuesta sin calcular nada. La línea de
comandos se prueba con un embedder falso: no hay red ni clave.
"""

import ast
import logging
import math
from pathlib import Path

import httpx
import pytest
from openai import AuthenticationError, RateLimitError

from scripts import compare
from scripts.compare import cosine_similarity, main

LOGGER = "embedding_embedder"


def api_error(cls, status: int):
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    return cls("error de prueba", response=httpx.Response(status, request=request), body=None)


class FakeEmbedder:
    """Devuelve el vector que se le haya asignado a cada texto."""

    def __init__(self, vectors: dict[str, list[float]] | None = None, error: Exception | None = None):
        self.vectors = vectors or {}
        self.error = error
        self.asked: list[str] = []

    def embed_one(self, text: str) -> list[float]:
        self.asked.append(text)
        if self.error:
            raise self.error
        return self.vectors[text]


# ---------------------------------------------------------------------------
# El coseno a mano
# ---------------------------------------------------------------------------

def test_identical_vectors_give_one():
    assert cosine_similarity([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)


def test_perpendicular_vectors_give_zero():
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)


def test_opposite_vectors_give_minus_one():
    assert cosine_similarity([1, 2], [-1, -2]) == pytest.approx(-1.0)


def test_a_known_value():
    # (1·4 + 2·5 + 3·6) / (√14 · √77) = 32 / √1078
    assert cosine_similarity([1, 2, 3], [4, 5, 6]) == pytest.approx(32 / math.sqrt(1078))


def test_the_length_of_the_vector_does_not_matter_only_its_direction():
    assert cosine_similarity([1, 2], [10, 20]) == pytest.approx(1.0)


def test_unit_vectors_give_just_the_dot_product():
    """Los de OpenAI miden 1: el coseno es la suma de productos."""
    a, b = [0.6, 0.8], [1.0, 0.0]
    assert cosine_similarity(a, b) == pytest.approx(0.6)


def test_vectors_of_different_length_are_an_error():
    with pytest.raises(ValueError, match="miden"):
        cosine_similarity([1, 2, 3], [1, 2])


def test_a_zero_vector_is_an_error():
    with pytest.raises(ValueError, match="ceros"):
        cosine_similarity([0, 0], [1, 2])


def test_the_script_uses_only_the_standard_library_for_the_math():
    """El enunciado prohíbe numpy y scikit-learn «solo para esto»."""
    tree = ast.parse(Path(compare.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & {"numpy", "sklearn", "scipy"}


# ---------------------------------------------------------------------------
# La línea de comandos
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def restore_embedder_log_level():
    """main() silencia el registro del embedder; que no afecte a otros tests."""
    logger = logging.getLogger(LOGGER)
    previous = logger.level
    yield
    logger.setLevel(previous)


def test_prints_the_two_texts_and_the_similarity_with_four_decimals(capsys):
    embedder = FakeEmbedder({"texto uno": [1.0, 0.0], "texto dos": [1.0, 1.0]})

    code = main(["--text-a", "texto uno", "--text-b", "texto dos"], embedder=embedder)

    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        "Text A: texto uno",
        "Text B: texto dos",
        "Cosine similarity: 0.7071",
    ]
    assert embedder.asked == ["texto uno", "texto dos"]


def test_accents_and_symbols_in_the_texts_are_kept(capsys):
    embedder = FakeEmbedder({"Backend de autenticación": [1.0, 0.0], "API — 100 %": [0.0, 1.0]})

    main(["--text-a", "Backend de autenticación", "--text-b", "API — 100 %"], embedder=embedder)

    out = capsys.readouterr().out
    assert "Text A: Backend de autenticación" in out
    assert "Cosine similarity: 0.0000" in out


@pytest.mark.parametrize("argv", [[], ["--text-a", "solo uno"], ["--text-b", "solo dos"]])
def test_both_texts_are_required(argv, capsys):
    with pytest.raises(SystemExit) as stopped:
        main(argv, embedder=FakeEmbedder())

    assert stopped.value.code == 2
    assert "--text" in capsys.readouterr().err


def test_an_empty_text_is_refused_before_calling_openai(capsys):
    embedder = FakeEmbedder()

    with pytest.raises(SystemExit) as stopped:
        main(["--text-a", "   ", "--text-b", "algo"], embedder=embedder)

    assert stopped.value.code == 2
    assert embedder.asked == []


def test_a_wrong_key_gives_a_clear_message_not_a_traceback(capsys):
    embedder = FakeEmbedder(error=api_error(AuthenticationError, 401))

    code = main(["--text-a", "a", "--text-b", "b"], embedder=embedder)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "OPENAI_API_KEY" in captured.err and "Traceback" not in captured.err


def test_any_other_openai_error_gives_a_short_message(capsys):
    embedder = FakeEmbedder(error=api_error(RateLimitError, 429))

    code = main(["--text-a", "a", "--text-b", "b"], embedder=embedder)

    assert code == 1
    assert "RateLimitError" in capsys.readouterr().err


def test_the_per_call_log_is_silenced_unless_verbose_is_asked():
    embedder = FakeEmbedder({"a": [1.0, 0.0], "b": [0.0, 1.0]})
    logger = logging.getLogger(LOGGER)

    logger.setLevel(logging.INFO)
    main(["--text-a", "a", "--text-b", "b"], embedder=embedder)
    assert logger.level == logging.WARNING

    logger.setLevel(logging.INFO)
    main(["--text-a", "a", "--text-b", "b", "--verbose"], embedder=embedder)
    assert logger.level == logging.INFO
