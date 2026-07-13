import __main__
import io
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter

import reader3
import server
from importers import (
    DocumentImportError,
    _PdfBookmark,
    _extract_pdf_captions,
    _extract_pdf_pages,
    _normalize_pdf_page_text,
    _sanitize_html,
    _split_pdf_section_texts,
    _validate_public_url,
    import_file,
)


for class_name in ("Book", "BookMetadata", "ChapterContent", "TOCEntry"):
    setattr(__main__, class_name, getattr(reader3, class_name))


def make_pdf(page_count=3, with_outline=False):
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    if with_outline:
        chapter = writer.add_outline_item("Chapter one", 0)
        writer.add_outline_item("First topic", 1, parent=chapter)
        writer.add_outline_item("Chapter two", 2)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class ImporterTests(unittest.TestCase):
    def test_pdf_sections_split_multiple_headings_on_the_same_page(self):
        entries = [
            _PdfBookmark(title="1 Parent", page=1, href="parent"),
            _PdfBookmark(title="1.1 Child", page=1, href="child"),
            _PdfBookmark(title="2 Next", page=2, href="next"),
        ]
        texts, ranges = _split_pdf_section_texts(
            ["1 Parent Parent introduction. 1.1 Child Child details only.", "2 Next Next body."],
            entries,
            2,
        )

        self.assertEqual(ranges, [(1, 1), (1, 1), (2, 2)])
        self.assertIn("Parent introduction.", texts[0])
        self.assertNotIn("Child details", texts[0])
        self.assertIn("Child details only.", texts[1])
        self.assertNotIn("Parent introduction", texts[1])

    def test_pdf_page_uses_ocr_when_embedded_text_is_missing(self):
        page = type("Page", (), {"extract_text": lambda self: ""})()
        reader = type("Reader", (), {"pages": [page]})()
        with patch("importers._ocr_pdf_page", return_value="Recovered scanned text") as ocr:
            extracted = _extract_pdf_pages(reader)

        ocr.assert_called_once_with(page)
        self.assertEqual(extracted, ["Recovered scanned text"])

    def test_pdf_figure_caption_wraps_hyphenated_line(self):
        text = "Figure 2.3 Illustration of the accura-\ncy of the classifier.\nBody text follows."
        page = type("Page", (), {"extract_text": lambda self: text})()
        reader = type("Reader", (), {"pages": [page]})()
        self.assertEqual(
            _extract_pdf_captions(reader),
            {1: ["Figure 2.3 Illustration of the accuracy of the classifier."]},
        )

    def test_html_is_sanitized_and_imported(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "article.html"
            source.write_text(
                '<html><head><title>Test article</title></head><body>'
                '<script>alert(1)</script><h1>Hello</h1>'
                '<a href="javascript:alert(1)" onclick="alert(1)">bad</a>'
                '<p>First paragraph.</p><p>Second paragraph.</p>'
                '<font>Third paragraph.<br><br>Fourth paragraph.</font>'
                '<img src="/figure.png"></body></html>',
                encoding="utf-8",
            )
            book_id = import_file(source, root, source_url="https://example.com/posts/test")
            with (Path(root) / book_id / "book.pkl").open("rb") as handle:
                book = pickle.load(handle)

            self.assertEqual(book.metadata.title, "Test article")
            self.assertEqual(book.document_type, "url")
            self.assertNotIn("script", book.spine[0].content)
            self.assertNotIn("javascript:", book.spine[0].content)
            self.assertNotIn("onclick", book.spine[0].content)
            self.assertIn("https://example.com/figure.png", book.spine[0].content)
            self.assertIn("First paragraph.\n\nSecond paragraph.", book.spine[0].text)
            self.assertIn("Third paragraph.\n\nFourth paragraph.", book.spine[0].text)

    def test_image_is_preserved_as_an_asset(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "diagram.png"
            payload = b"\x89PNG\r\n\x1a\nreader3-test"
            source.write_bytes(payload)
            book_id = import_file(source, root)
            asset = Path(root) / book_id / "assets" / "document.png"
            self.assertEqual(asset.read_bytes(), payload)

    def test_private_url_is_rejected(self):
        with self.assertRaises(DocumentImportError):
            _validate_public_url("http://127.0.0.1/private.pdf")

    def test_sanitizer_keeps_safe_links(self):
        _, content, _ = _sanitize_html('<a href="https://example.com">safe</a>')
        self.assertIn('href="https://example.com"', content)

    def test_pdf_outline_becomes_toc_and_page_sections(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "outlined.pdf"
            source.write_bytes(make_pdf(page_count=4, with_outline=True))
            book_id = import_file(source, root)
            with (Path(root) / book_id / "book.pkl").open("rb") as handle:
                book = pickle.load(handle)

            self.assertEqual(book.document_type, "pdf")
            self.assertEqual(book.page_count, 4)
            self.assertEqual([section.start_page for section in book.spine], [1, 2, 3])
            self.assertEqual([section.end_page for section in book.spine], [1, 2, 4])
            self.assertEqual(book.toc[0].title, "Chapter one")
            self.assertEqual(book.toc[0].children[0].title, "First topic")

    def test_pdf_same_page_bookmarks_keep_distinct_toc_targets(self):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.add_blank_page(width=612, height=792)
        parent = writer.add_outline_item("Parent", 0)
        writer.add_outline_item("Child", 0, parent=parent)
        payload = io.BytesIO()
        writer.write(payload)

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "same-page.pdf"
            source.write_bytes(payload.getvalue())
            book_id = import_file(source, root)
            with (Path(root) / book_id / "book.pkl").open("rb") as handle:
                book = pickle.load(handle)

            self.assertEqual(len(book.spine), 2)
            self.assertNotEqual(book.toc[0].file_href, book.toc[0].children[0].file_href)
            self.assertEqual([section.start_page for section in book.spine], [1, 1])

    def test_pdf_without_outline_uses_page_ranges(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "plain.pdf"
            source.write_bytes(make_pdf(page_count=12))
            book_id = import_file(source, root)
            with (Path(root) / book_id / "book.pkl").open("rb") as handle:
                book = pickle.load(handle)

            self.assertEqual([section.start_page for section in book.spine], [1, 11])
            self.assertEqual([section.end_page for section in book.spine], [10, 12])

    def test_pdf_raster_figures_are_extracted_by_page(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "figure.pdf"
            image = Image.effect_noise((320, 320), 100).convert("RGB")
            image.save(source, "PDF", resolution=144)
            book_id = import_file(source, root)
            with (Path(root) / book_id / "book.pkl").open("rb") as handle:
                book = pickle.load(handle)

            self.assertEqual(len(book.spine[0].media), 1)
            extracted = Path(root) / book_id / book.spine[0].media[0]
            self.assertTrue(extracted.is_file())
            self.assertGreater(extracted.stat().st_size, 5_000)

    def test_context_includes_section_and_source_pages(self):
        book = reader3.Book(
            metadata=reader3.BookMetadata(title="A book", language="en", authors=["An Author"]),
            spine=[], toc=[], images={}, source_file="book.pdf", processed_at="now",
        )
        chapter = reader3.ChapterContent(
            id="one", href="one", title="A section", content="", text="Important text.", order=0,
            start_page=4, end_page=7,
        )
        context = reader3.format_section_context(book, chapter)
        self.assertIn("Section: A section", context)
        self.assertIn("Source pages: 4-7", context)
        self.assertIn("Important text.", context)

    def test_pdf_text_normalization_removes_layout_noise(self):
        raw = (
            "2 1. THE DEEP LEARNING REVOLUTION\n"
            "A long sen-\ntence continues here and mentions modelsChapter 12\n"
            "1© The Author(s), under exclusive license to Springer Nature Switzerland AG 2024\n"
            "https://doi.org/10.1007/example\n"
        )
        normalized = _normalize_pdf_page_text(raw)
        self.assertNotIn("THE DEEP LEARNING REVOLUTION", normalized)
        self.assertIn("A long sentence", normalized)
        self.assertNotIn("Chapter 12", normalized)
        self.assertNotIn("doi.org", normalized)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_books_dir = server.BOOKS_DIR
        server.BOOKS_DIR = self.temp_dir.name
        server.load_book_cached.cache_clear()
        self.client = TestClient(server.app)

    def tearDown(self):
        self.client.close()
        server.BOOKS_DIR = self.previous_books_dir
        server.load_book_cached.cache_clear()
        self.temp_dir.cleanup()

    def test_pdf_upload_opens_original_asset(self):
        payload = make_pdf(with_outline=True)
        response = self.client.post(
            "/import/upload",
            files={"file": ("sample.pdf", payload, "application/pdf")},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        reader_url = response.headers["location"]
        reader_response = self.client.get(reader_url)
        self.assertEqual(reader_response.status_code, 200)
        self.assertIn('id="text-mode-button"', reader_response.text)
        self.assertIn("navigatePdfSection", reader_response.text)
        self.assertIn('value="chatgpt-web"', reader_response.text)
        self.assertIn('value="claude-web"', reader_response.text)
        self.assertIn('value="google"', reader_response.text)
        self.assertIn('value="apple-foundation"', reader_response.text)
        self.assertIn('id="chat-panel"', reader_response.text)
        self.assertIn("modelSelectionChanged()", reader_response.text)
        self.assertNotIn("toggleChatPanel", reader_response.text)
        self.assertNotIn(">Chat<", reader_response.text)
        self.assertNotIn(">Copy Markdown<", reader_response.text)
        self.assertNotIn(">Copy selection<", reader_response.text)
        self.assertIn("runPromptAction('explain')", reader_response.text)
        self.assertIn("copyTocSection", reader_response.text)
        self.assertNotIn('value="gemini-chrome"', reader_response.text)
        self.assertNotIn("/api/gemini/chrome", reader_response.text)
        self.assertIn("frame.replaceWith(nextFrame)", reader_response.text)
        book_id = reader_url.split("/")[2]
        first_section_response = self.client.get(f"/read/{book_id}")
        self.assertEqual(first_section_response.status_code, 200)
        asset_response = self.client.get(f"/read/{book_id}/asset")
        self.assertEqual(asset_response.status_code, 200)
        self.assertEqual(asset_response.content, payload)
        context_response = self.client.get(f"/api/read/{book_id}/0/context")
        self.assertEqual(context_response.status_code, 200)
        context = context_response.json()
        self.assertIn("text", context)
        self.assertIn("markdown", context)
        self.assertNotIn("## Page", context["markdown"])
        self.assertEqual(context["media"], [])

        chatgpt_response = self.client.get(
            f"/open/chatgpt/{book_id}/0",
            params={"preferences": "常に日本語で回答してください。"},
            follow_redirects=False,
        )
        self.assertEqual(chatgpt_response.status_code, 303)
        self.assertTrue(chatgpt_response.headers["location"].startswith("https://chatgpt.com/?q="))
        self.assertLess(len(chatgpt_response.headers["location"].encode()), 2_048)
        self.assertIn("常に日本語で回答してください。", unquote(chatgpt_response.headers["location"]))

        claude_response = self.client.get(
            f"/open/claude/{book_id}/0", follow_redirects=False
        )
        self.assertEqual(claude_response.status_code, 303)
        self.assertTrue(claude_response.headers["location"].startswith("https://claude.ai/new?q="))

    def test_llm_status_and_in_page_chat_api(self):
        status_response = self.client.get("/api/llm/status")
        self.assertEqual(status_response.status_code, 200)
        self.assertIn("apple_foundation", status_response.json())
        self.assertIn("google", status_response.json()["providers"])

        mocked_call = AsyncMock(return_value="A grounded answer")
        with patch("server.call_llm", mocked_call):
            response = self.client.post(
                "/api/llm/chat",
                json={
                    "provider": "google",
                    "model": "gemini-3.5-flash",
                    "api_token": "test-token",
                    "instructions": "Use the supplied text.",
                    "messages": [{"role": "user", "content": "Explain this."}],
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "A grounded answer")
        self.assertEqual(mocked_call.await_args.args[0].api_token, "test-token")

    def test_ai_handoff_limits_encoded_query_size(self):
        prompt = "Explain this section. " + ("A sentence with spaces & symbols. " * 2_000)
        encoded = server.encode_ai_url_prompt(prompt)
        self.assertLessEqual(len(encoded), server.AI_URL_MAX_ENCODED_CHARS)
        self.assertIn("avoid a browser 431 error", unquote(encoded))

    def test_my_page_contains_global_provider_settings(self):
        response = self.client.get("/me")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="openai-token"', response.text)
        self.assertIn('id="google-token"', response.text)
        self.assertIn('id="compatible-url"', response.text)
        self.assertIn('id="response-instructions"', response.text)
        self.assertIn('responseInstructions: ""', response.text)
        self.assertIn('id="prompt-list"', response.text)

    def test_library_contains_upload_controls(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/import/upload"', response.text)
        self.assertIn('action="/import/url"', response.text)

    def test_url_form_imports_downloaded_html(self):
        def fake_download(url, destination_dir):
            destination = Path(destination_dir) / "page.html"
            destination.write_text("<title>Remote article</title><h1>Remote article</h1>", encoding="utf-8")
            return destination, "https://example.com/page"

        with patch("server.download_url", side_effect=fake_download):
            response = self.client.post(
                "/import/url",
                data={"url": "https://example.com/page"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        reader_response = self.client.get(response.headers["location"])
        self.assertEqual(reader_response.status_code, 200)
        self.assertIn('id="html-mode-button"', reader_response.text)
        self.assertIn('id="text-mode-button"', reader_response.text)
        self.assertIn('onclick="setViewMode(\'html\')"', reader_response.text)
        context_response = self.client.get(response.headers["location"].replace("/read/", "/api/read/") + "/context")
        self.assertEqual(context_response.status_code, 200)
        self.assertIn("Remote article", context_response.json()["context"])


if __name__ == "__main__":
    unittest.main()
