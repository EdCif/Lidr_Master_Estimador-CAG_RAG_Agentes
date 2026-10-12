"""Pruebas de los adjuntos: validación común, Camino A y Camino B.

Sin red ni claves: el cliente de Anthropic y litellm se sustituyen por
dobles de prueba que graban lo que se les pide y devuelven respuestas fijas.
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# litellm intenta descargar su tabla de precios al importarse; en los tests
# le pedimos que use la copia local que trae instalada.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import anthropic
import httpx2

from app.services.attachments import path_a_files_api, path_b_local
from app.services.attachments.common import (
    Attachment,
    AttachmentError,
    LLMProviderError,
    ReplyStats,
    detect_kind,
    sanitize_filename,
    validate_attachment,
)
from doc_factories import make_blank_pdf, make_docx, make_text_pdf


# El Word de prueba se fabrica una sola vez, al cargar el módulo: python-docx
# tarda una décima de segundo en crearlo y así no lo repetimos en cada test.
SPEC_DOCX = make_docx(
    "Especificación técnica",
    ["El backend se desplegará en Kubernetes.", "La base de datos será PostgreSQL."],
    table=[["Módulo", "Horas"], ["Reservas", "40"]],
)


def docx_attachment(name="spec.docx"):
    return Attachment(filename=name, kind="docx", data=SPEC_DOCX)


def pdf_attachment(text="Arquitectura con Kubernetes y Kafka", name="arquitectura.pdf"):
    return Attachment(filename=name, kind="pdf", data=make_text_pdf(text))


async def collect(async_iterator):
    return [item async for item in async_iterator]


# ---------------------------------------------------------------------------
# Validación común
# ---------------------------------------------------------------------------

class CommonValidationTests(unittest.TestCase):
    def test_filenames_lose_paths_and_control_characters(self):
        self.assertEqual(sanitize_filename("C:\\docs\\spec.pdf"), "spec.pdf")
        self.assertEqual(sanitize_filename("../../etc/spec.pdf"), "spec.pdf")
        self.assertEqual(sanitize_filename("spec\n--- attachment: x ---.pdf"), "spec--- attachment: x ---.pdf")
        self.assertEqual(sanitize_filename(""), "adjunto")
        self.assertEqual(len(sanitize_filename("a" * 300 + ".pdf")), 120)

    def test_pdf_and_docx_are_detected_by_extension_and_content(self):
        self.assertEqual(detect_kind("a.pdf", make_text_pdf("hola")), "pdf")
        self.assertEqual(detect_kind("a.DOCX", make_docx("T", ["p"])), "docx")

    def test_unsupported_or_disguised_files_are_rejected_with_415(self):
        cases = [
            ("notas.txt", b"hola"),
            ("viejo.doc", b"\xd0\xcf\x11\xe0 binario de Word 97"),
            ("falso.pdf", b"esto no es un pdf"),
            ("falso.docx", make_text_pdf("pdf disfrazado")),
        ]
        for filename, data in cases:
            with self.subTest(filename=filename), self.assertRaises(AttachmentError) as context:
                detect_kind(filename, data)
            self.assertEqual(context.exception.status_code, 415)

    def test_empty_and_oversized_files_are_rejected(self):
        with self.assertRaises(AttachmentError) as empty:
            validate_attachment("a.pdf", b"", max_bytes=100)
        self.assertEqual(empty.exception.status_code, 422)

        with self.assertRaises(AttachmentError) as big:
            validate_attachment("a.pdf", b"%PDF-" + b"x" * 200, max_bytes=100)
        self.assertEqual(big.exception.status_code, 413)

    def test_valid_file_becomes_an_attachment(self):
        attachment = validate_attachment("../spec.pdf", make_text_pdf("hola"), max_bytes=10_000)

        self.assertEqual(attachment.filename, "spec.pdf")
        self.assertEqual(attachment.kind, "pdf")
        self.assertEqual(attachment.media_type, "application/pdf")

    # --- Topes que se miran antes de abrir el fichero (Paso 00-bis) ---------

    def test_pdf_with_too_many_pages_is_rejected_before_extracting_text(self):
        """Un PDF de 3 páginas con tope de 2 se corta con 413; con tope 3, pasa."""
        three_pages = make_blank_pdf(pages=3)

        with self.assertRaises(AttachmentError) as context:
            validate_attachment("largo.pdf", three_pages, max_bytes=1_000_000, max_pdf_pages=2)
        self.assertEqual(context.exception.status_code, 413)
        self.assertIn("3 páginas", str(context.exception))

        attachment = validate_attachment("largo.pdf", three_pages, max_bytes=1_000_000, max_pdf_pages=3)
        self.assertEqual(attachment.kind, "pdf")

    def test_docx_that_would_inflate_too_much_is_rejected_without_opening_it(self):
        """Lo que mide es el Word DESCOMPRIMIDO, no los bytes que llegan.

        El .docx de prueba ocupa unos pocos KB comprimido. Le ponemos un tope
        de descomprimido justo por debajo de su tamaño real: tiene que
        rechazarse aunque sus bytes estén muy por debajo de max_bytes.
        """
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(SPEC_DOCX)) as archive:
            real_size = sum(info.file_size for info in archive.infolist())

        with self.assertRaises(AttachmentError) as context:
            validate_attachment(
                "bomba.docx", SPEC_DOCX, max_bytes=10_000_000, max_docx_uncompressed_bytes=real_size - 1
            )
        self.assertEqual(context.exception.status_code, 413)

        attachment = validate_attachment(
            "normal.docx", SPEC_DOCX, max_bytes=10_000_000, max_docx_uncompressed_bytes=real_size
        )
        self.assertEqual(attachment.kind, "docx")

    def test_broken_pdf_or_docx_is_rejected_with_422(self):
        """Empiezan como un PDF o un ZIP de verdad, pero por dentro están rotos."""
        cases = [
            ("roto.pdf", b"%PDF-1.4\nesto no es un PDF de verdad"),
            ("roto.docx", b"PK\x03\x04" + b"esto no es un zip de verdad" * 10),
        ]
        for filename, data in cases:
            with self.subTest(filename=filename), self.assertRaises(AttachmentError) as context:
                validate_attachment(filename, data, max_bytes=1_000_000)
            self.assertEqual(context.exception.status_code, 422)


# ---------------------------------------------------------------------------
# Camino B · extracción local
# ---------------------------------------------------------------------------

class PathBExtractionTests(unittest.TestCase):
    def test_pypdf_extracts_text_from_a_text_pdf(self):
        text, method = path_b_local.extract_text(pdf_attachment(), "pypdf")

        self.assertIn("Kubernetes", text)
        self.assertEqual(method, "pypdf")

    def test_markitdown_can_be_chosen_for_pdfs(self):
        text, method = path_b_local.extract_text(pdf_attachment(), "markitdown")

        self.assertIn("Kubernetes", text)
        self.assertEqual(method, "MarkItDown")

    def test_pdf_without_text_suggests_path_a(self):
        attachment = Attachment(filename="escaneado.pdf", kind="pdf", data=make_blank_pdf())

        with self.assertRaises(AttachmentError) as context:
            path_b_local.extract_text(attachment, "pypdf")
        self.assertIn("files_api", str(context.exception))

    def test_docx_keeps_headings_paragraphs_and_tables_in_order(self):
        text, method = path_b_local.extract_text(docx_attachment(), "pypdf")

        self.assertEqual(method, "python-docx")
        self.assertEqual(
            text.splitlines()[:5],
            [
                "# Especificación técnica",
                "El backend se desplegará en Kubernetes.",
                "La base de datos será PostgreSQL.",
                "Módulo | Horas",
                "Reservas | 40",
            ],
        )

    def test_prompt_uses_a_clear_separator_per_attachment(self):
        prompt = path_b_local.build_prompt_with_attachments(
            "  Transcripción de la reunión.  ",
            [("spec.pdf", "Texto del PDF"), ("propuesta.docx", "Texto del Word\n")],
        )

        self.assertEqual(
            prompt,
            "Transcripción de la reunión.\n\n"
            "--- attachment: spec.pdf ---\nTexto del PDF\n\n"
            "--- attachment: propuesta.docx ---\nTexto del Word",
        )

    def test_prepare_user_content_returns_plain_text_and_notes(self):
        prepared = path_b_local.prepare_user_content(
            "Queremos un portal de reservas.", [pdf_attachment(), docx_attachment()]
        )

        self.assertIsInstance(prepared.user_content, str)
        self.assertIn("--- attachment: arquitectura.pdf ---", prepared.user_content)
        self.assertIn("--- attachment: spec.docx ---", prepared.user_content)
        self.assertEqual(len(prepared.notes), 2)
        self.assertEqual(prepared.uploaded_files, [])

    def test_too_much_attachment_text_is_rejected_not_silently_truncated(self):
        with self.assertRaises(AttachmentError) as context:
            path_b_local.prepare_user_content("Reunión", [docx_attachment()], max_chars=10)
        self.assertEqual(context.exception.status_code, 413)

    def test_block_messages_from_path_a_are_flattened_to_text(self):
        messages = [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": [
                {"type": "document", "source": {"type": "file", "file_id": "f1"}, "title": "spec.pdf"},
                {"type": "text", "text": "Estima esto"},
            ]},
            {"role": "assistant", "content": "80 horas"},
        ]

        flattened = path_b_local.to_text_only_messages(messages)

        self.assertEqual(
            flattened[1]["content"],
            "[documento adjunto en un turno anterior: spec.pdf]\n\nEstima esto",
        )
        self.assertEqual(flattened[0], messages[0])
        self.assertEqual(flattened[2], messages[2])


def litellm_chunk(text=None, finish_reason=None, usage=None):
    choices = [] if text is None and finish_reason is None else [
        SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish_reason)
    ]
    return SimpleNamespace(choices=choices, usage=usage)


async def async_iter(items):
    for item in items:
        yield item


class PathBStreamingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Importar litellm tarda unos segundos. Lo hacemos aquí, fuera del
        # bucle de eventos del test, para que asyncio no avise de una tarea lenta.
        import litellm  # noqa: F401

    async def test_streams_text_and_fills_stats(self):
        chunks = [
            litellm_chunk("Estimación: "),
            litellm_chunk("80 horas", finish_reason="stop"),
            litellm_chunk(usage=SimpleNamespace(prompt_tokens=120, completion_tokens=8)),
        ]
        stats = ReplyStats(model="claude-opus-5-5")
        messages = [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "Hola"}]

        with patch("litellm.acompletion", new=AsyncMock(return_value=async_iter(chunks))) as acompletion:
            pieces = await collect(path_b_local.stream_reply(
                messages, model="claude-opus-5-5", api_key="test-key", max_tokens=1000, stats=stats
            ))

        self.assertEqual(pieces, ["Estimación: ", "80 horas"])
        self.assertEqual(stats.final_text, "Estimación: 80 horas")
        self.assertEqual((stats.input_tokens, stats.output_tokens), (120, 8))
        self.assertIsNone(stats.incomplete_reason)
        kwargs = acompletion.await_args.kwargs
        self.assertEqual(kwargs["model"], "anthropic/claude-opus-5-5")
        self.assertEqual(kwargs["messages"], messages)
        self.assertTrue(kwargs["stream"])

    async def test_cut_by_token_limit_is_marked_incomplete(self):
        stats = ReplyStats(model="m")
        chunks = [litellm_chunk("Estimación a me", finish_reason="length")]

        with patch("litellm.acompletion", new=AsyncMock(return_value=async_iter(chunks))):
            await collect(path_b_local.stream_reply(
                [{"role": "system", "content": "S"}], model="m", api_key="k", max_tokens=10, stats=stats
            ))

        self.assertIn("límite de tokens", stats.incomplete_reason)


# ---------------------------------------------------------------------------
# Camino A · Files API (con un cliente de Anthropic de mentira)
# ---------------------------------------------------------------------------

class FakeFiles:
    def __init__(self):
        self.uploads = []

    async def upload(self, *, file, expires_in_seconds):
        self.uploads.append({"file": file, "expires_in_seconds": expires_in_seconds})
        return SimpleNamespace(id=f"file_{len(self.uploads)}")


class FakeStream:
    def __init__(self, chunks, final_message):
        self.chunks = chunks
        self.final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    @property
    def text_stream(self):
        return async_iter(self.chunks)

    async def get_final_message(self):
        return self.final_message


def final_message(text="Estimación: 80 horas", stop_reason="end_turn"):
    return SimpleNamespace(
        model="claude-opus-5-5",
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=300, output_tokens=12),
        # Opus 5.5 devuelve también un bloque de razonamiento: hay que ignorarlo.
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )


def fake_anthropic_client(chunks=("Estimación: ", "80 horas"), final=None):
    stream_calls = []

    def stream(**kwargs):
        stream_calls.append(kwargs)
        return FakeStream(list(chunks), final or final_message())

    client = SimpleNamespace(
        files=FakeFiles(),
        beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)),
    )
    return client, stream_calls


class PathAPrepareTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_attachments_the_message_is_the_plain_transcript(self):
        client, _ = fake_anthropic_client()

        prepared = await path_a_files_api.prepare_user_content(client, "Solo texto", [])

        self.assertEqual(prepared.user_content, "Solo texto")
        self.assertEqual(client.files.uploads, [])

    async def test_pdf_is_uploaded_as_is_and_referenced_by_file_id(self):
        client, _ = fake_anthropic_client()
        attachment = pdf_attachment()

        prepared = await path_a_files_api.prepare_user_content(client, "Estima esto", [attachment])

        upload = client.files.uploads[0]
        self.assertEqual(upload["file"], ("arquitectura.pdf", attachment.data, "application/pdf"))
        self.assertEqual(upload["expires_in_seconds"], 24 * 60 * 60)
        self.assertEqual(
            prepared.user_content,
            [
                {"type": "document", "source": {"type": "file", "file_id": "file_1"}, "title": "arquitectura.pdf"},
                {"type": "text", "text": "Estima esto"},
            ],
        )
        self.assertEqual(prepared.uploaded_files, [{"filename": "arquitectura.pdf", "file_id": "file_1"}])

    async def test_docx_is_converted_to_text_locally_and_uploaded_as_txt(self):
        client, _ = fake_anthropic_client()

        prepared = await path_a_files_api.prepare_user_content(client, "Estima", [docx_attachment()])

        name, data, media_type = client.files.uploads[0]["file"]
        self.assertEqual((name, media_type), ("spec.docx.txt", "text/plain"))
        self.assertIn("Kubernetes", data.decode("utf-8"))
        self.assertEqual(prepared.user_content[0]["title"], "spec.docx")
        self.assertIn("no acepta .docx", prepared.notes[0])

    async def test_docx_text_over_the_limit_is_rejected_before_uploading_anything(self):
        """Mismo límite de caracteres que el Camino B, y sin subir nada a Anthropic."""
        client, _ = fake_anthropic_client()

        with self.assertRaises(AttachmentError) as context:
            await path_a_files_api.prepare_user_content(
                client, "Estima", [pdf_attachment(), docx_attachment()], max_chars=10
            )

        self.assertEqual(context.exception.status_code, 413)
        self.assertEqual(client.files.uploads, [])

    async def test_connection_errors_become_a_readable_provider_error(self):
        client, _ = fake_anthropic_client()
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/files")
        client.files.upload = AsyncMock(side_effect=anthropic.APIConnectionError(request=request))

        with self.assertRaises(LLMProviderError) as context:
            await path_a_files_api.prepare_user_content(client, "Estima", [pdf_attachment()])
        self.assertIn("conectar con Anthropic", str(context.exception))


class PathAStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_system_prompt_goes_apart_and_fallbacks_are_enabled(self):
        client, calls = fake_anthropic_client()
        stats = ReplyStats(model="claude-opus-5-5")
        messages = [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "Estima esto"},
        ]

        pieces = await collect(path_a_files_api.stream_reply(
            client, messages, model="claude-opus-5-5", effort="medium", max_tokens=64000, stats=stats
        ))

        self.assertEqual(pieces, ["Estimación: ", "80 horas"])
        call = calls[0]
        self.assertEqual(call["system"], "SYSTEM")
        self.assertEqual(call["messages"], [{"role": "user", "content": "Estima esto"}])
        self.assertEqual(call["output_config"], {"effort": "medium"})
        self.assertEqual(call["fallbacks"], "default")
        self.assertEqual(call["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(stats.final_text, "Estimación: 80 horas")
        self.assertEqual((stats.input_tokens, stats.output_tokens), (300, 12))
        self.assertIsNone(stats.incomplete_reason)

    async def test_refusal_is_marked_incomplete(self):
        client, _ = fake_anthropic_client(chunks=[], final=final_message(text="", stop_reason="refusal"))
        stats = ReplyStats(model="claude-opus-5-5")

        await collect(path_a_files_api.stream_reply(
            client, [{"role": "system", "content": "S"}, {"role": "user", "content": "x"}],
            model="claude-opus-5-5", effort="medium", max_tokens=1000, stats=stats,
        ))

        self.assertIn("rechazado", stats.incomplete_reason)


if __name__ == "__main__":
    unittest.main()
