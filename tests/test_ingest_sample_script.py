"""Pruebas de scripts/ingest_sample.py (sesión 7).

El script manda el dataset al endpoint y guarda la respuesta. Aquí el
«servidor» es falso: ``httpx.MockTransport`` contesta lo que cada test quiere
sin abrir ningún puerto ni llamar a OpenAI.
"""

import json
from pathlib import Path

import httpx

from scripts import ingest_sample
from scripts.ingest_sample import DEFAULT_INPUT, DEFAULT_OUTPUT, DEFAULT_URL, main, summarize

FAKE_RESPONSE = {
    "chunks": [
        {
            "chunk_id": "BUD-2024-001::AUTH-001",
            "text": "texto",
            "metadata": {"client_sector": "finance"},
            "token_count": 100,
            "embedding": [0.1, 0.2, 0.3],
        },
        {
            "chunk_id": "BUD-2024-002::KYC-002",
            "text": "texto",
            "metadata": {"client_sector": "finance"},
            "token_count": 110,
            "embedding": [0.3, 0.2, 0.1],
        },
        {
            "chunk_id": "BUD-2023-005::CART-001",
            "text": "texto",
            "metadata": {"client_sector": "ecommerce"},
            "token_count": 90,
            "embedding": [0.5, 0.5, 0.5],
        },
    ],
    "stats": {"total_budgets": 2, "total_chunks": 3, "total_tokens": 300, "estimated_cost_usd": 0.000006},
}


def client_answering(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sends_the_whole_dataset_wrapped_in_budgets_to_the_endpoint(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=FAKE_RESPONSE)

    code = main(["--output", str(tmp_path / "salida.json")], client=client_answering(handler))

    assert code == 0
    assert seen["method"] == "POST" and seen["url"] == DEFAULT_URL
    assert list(seen["body"]) == ["budgets"]
    assert len(seen["body"]["budgets"]) == 15  # el dataset de ejemplo entero


def test_saves_the_response_to_the_output_file(tmp_path):
    output = tmp_path / "carpeta_nueva" / "salida.json"

    main(["--output", str(output)], client=client_answering(lambda request: httpx.Response(200, json=FAKE_RESPONSE)))

    assert json.loads(output.read_text(encoding="utf-8")) == FAKE_RESPONSE


def test_prints_a_summary_with_chunks_tokens_cost_and_vector_size(tmp_path, capsys):
    main(
        ["--output", str(tmp_path / "salida.json")],
        client=client_answering(lambda request: httpx.Response(200, json=FAKE_RESPONSE)),
    )

    out = capsys.readouterr().out
    assert "Chunks recibidos:      3" in out
    assert "Tokens totales:        300" in out
    assert "Coste estimado:        0.000006 USD" in out
    assert "Números por vector:    3" in out
    assert "finance 2, ecommerce 1" not in out  # el orden es alfabético
    assert "ecommerce 1, finance 2" in out


def test_summarize_reports_every_vector_size_found():
    result = json.loads(json.dumps(FAKE_RESPONSE))
    result["chunks"][0]["embedding"] = [0.1, 0.2]

    lines = summarize(result, seconds=1.5)

    assert any("Números por vector:    2, 3" in line for line in lines)
    assert any("Tiempo:                1.5 s" in line for line in lines)


def test_a_custom_url_is_used(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=FAKE_RESPONSE)

    main(
        ["--url", "http://localhost:9999/embeddings/ingest", "--output", str(tmp_path / "s.json")],
        client=client_answering(handler),
    )

    assert seen == ["http://localhost:9999/embeddings/ingest"]


def test_api_not_running_gives_a_clear_message(tmp_path, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no hay nadie", request=request)

    code = main(["--output", str(tmp_path / "s.json")], client=client_answering(handler))

    captured = capsys.readouterr()
    assert code == 1
    assert "uvicorn" in captured.err and "Traceback" not in captured.err
    assert not (tmp_path / "s.json").exists()


def test_a_slow_api_gives_a_timeout_message(tmp_path, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("tarda mucho", request=request)

    code = main(["--output", str(tmp_path / "s.json"), "--timeout", "5"], client=client_answering(handler))

    assert code == 1
    assert "5 segundos" in capsys.readouterr().err


def test_an_error_from_the_api_is_reported_and_nothing_is_saved(tmp_path, capsys):
    output = tmp_path / "s.json"
    handler = lambda request: httpx.Response(422, json={"detail": "La petición suma 60000 tokens"})  # noqa: E731

    code = main(["--output", str(output)], client=client_answering(handler))

    captured = capsys.readouterr()
    assert code == 1
    assert "422" in captured.err and "60000" in captured.err
    assert not output.exists()


def test_default_paths_point_to_the_data_folder():
    root = Path(ingest_sample.__file__).resolve().parent.parent
    assert DEFAULT_INPUT == root / "data" / "budgets_sample.json"
    assert DEFAULT_OUTPUT == root / "data" / "embeddings_sample.json"
    assert DEFAULT_INPUT.is_file()
