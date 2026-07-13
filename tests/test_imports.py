import __main__
import io
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfWriter

import reader3
import server
from importers import (
    DocumentImportError,
    _normalize_pdf_page_text,
    _sanitize_html,
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
    def test_html_is_sanitized_and_imported(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "article.html"
            source.write_text(
                '<html><head><title>Test article</title></head><body>'
                '<script>alert(1)</script><h1>Hello</h1>'
                '<a href="javascript:alert(1)" onclick="alert(1)">bad</a>'
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

    def test_pdf_without_outline_uses_page_ranges(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "plain.pdf"
            source.write_bytes(make_pdf(page_count=12))
            book_id = import_file(source, root)
            with (Path(root) / book_id / "book.pkl").open("rb") as handle:
                book = pickle.load(handle)

            self.assertEqual([section.start_page for section in book.spine], [1, 11])
            self.assertEqual([section.end_page for section in book.spine], [10, 12])

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
        self.assertIn("https://chatgpt.com/?q=", reader_response.text)
        self.assertIn("https://claude.ai/new?q=", reader_response.text)
        self.assertIn("https://gemini.google.com/app?q=", reader_response.text)
        book_id = reader_url.split("/")[2]
        asset_response = self.client.get(f"/read/{book_id}/asset")
        self.assertEqual(asset_response.status_code, 200)
        self.assertEqual(asset_response.content, payload)
        context_response = self.client.get(f"/api/read/{book_id}/0/context")
        self.assertEqual(context_response.status_code, 200)
        self.assertIn("text", context_response.json())

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
        self.assertEqual(self.client.get(response.headers["location"]).status_code, 200)
        context_response = self.client.get(response.headers["location"].replace("/read/", "/api/read/") + "/context")
        self.assertEqual(context_response.status_code, 200)
        self.assertIn("Remote article", context_response.json()["context"])


if __name__ == "__main__":
    unittest.main()
