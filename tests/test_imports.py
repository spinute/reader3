import __main__
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import reader3
import server
from importers import DocumentImportError, _sanitize_html, _validate_public_url, import_file


for class_name in ("Book", "BookMetadata", "ChapterContent", "TOCEntry"):
    setattr(__main__, class_name, getattr(reader3, class_name))


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
        payload = b"%PDF-1.4\nreader3 test\n%%EOF"
        response = self.client.post(
            "/import/upload",
            files={"file": ("sample.pdf", payload, "application/pdf")},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        reader_url = response.headers["location"]
        self.assertEqual(self.client.get(reader_url).status_code, 200)
        book_id = reader_url.split("/")[2]
        asset_response = self.client.get(f"/read/{book_id}/asset")
        self.assertEqual(asset_response.status_code, 200)
        self.assertEqual(asset_response.content, payload)

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


if __name__ == "__main__":
    unittest.main()
