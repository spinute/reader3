"""Import EPUB, PDF, image, HTML, and web documents into reader3."""

from __future__ import annotations

import ipaddress
import mimetypes
import re
import shutil
import socket
from collections import Counter
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Comment
from pypdf import PdfReader

from reader3 import (
    Book,
    BookMetadata,
    ChapterContent,
    TOCEntry,
    process_epub,
    save_to_pickle,
)


MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
PDF_FALLBACK_SECTION_PAGES = 10
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
SUPPORTED_EXTENSIONS = {".epub", ".pdf", ".html", ".htm", *SUPPORTED_IMAGE_EXTENSIONS}


class DocumentImportError(ValueError):
    pass


@dataclass
class _PdfBookmark:
    title: str
    page: int
    href: str
    children: list["_PdfBookmark"] = field(default_factory=list)


def _slugify(value: str) -> str:
    value = unquote(value).strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:70] or "document"


def _unique_output_dir(output_root: Path, title_hint: str) -> Path:
    stem = _slugify(title_hint)
    candidate = output_root / f"{stem}_data"
    counter = 2
    while candidate.exists():
        candidate = output_root / f"{stem}-{counter}_data"
        counter += 1
    return candidate


def _metadata(title: str) -> BookMetadata:
    return BookMetadata(title=title.strip() or "Untitled", language="en")


def _asset_book(source: Path, output_dir: Path, document_type: str, source_url: str | None) -> Book:
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    extension = source.suffix.lower()
    asset_name = f"document{extension}"
    shutil.copy2(source, assets_dir / asset_name)
    title = source.stem.replace("_", " ").strip() or "Untitled"
    chapter = ChapterContent(
        id="document",
        href="document",
        title=title,
        content="",
        text="",
        order=0,
    )
    return Book(
        metadata=_metadata(title),
        spine=[chapter],
        toc=[TOCEntry(title=title, href="document", file_href="document", anchor="")],
        images={},
        source_file=source.name,
        processed_at=datetime.now().isoformat(),
        document_type=document_type,
        asset_filename=asset_name,
        source_url=source_url,
    )


def _pdf_outline(reader: PdfReader) -> list[_PdfBookmark]:
    """Convert pypdf's alternating destination/list outline into a stable tree."""
    counter = 0

    def convert(items) -> list[_PdfBookmark]:
        nonlocal counter
        result: list[_PdfBookmark] = []
        for item in items:
            if isinstance(item, list):
                if result:
                    result[-1].children = convert(item)
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:
                continue
            if page < 1 or page > len(reader.pages):
                continue
            title = " ".join(str(getattr(item, "title", "Untitled section")).split())
            counter += 1
            result.append(_PdfBookmark(title=title, page=page, href=f"pdf-section-{counter}"))
        return result

    try:
        return convert(reader.outline)
    except Exception:
        return []


def _flatten_bookmarks(bookmarks: list[_PdfBookmark]):
    for bookmark in bookmarks:
        yield bookmark
        yield from _flatten_bookmarks(bookmark.children)


def _bookmark_toc(bookmarks: list[_PdfBookmark], page_hrefs: dict[int, str]) -> list[TOCEntry]:
    return [
        TOCEntry(
            title=bookmark.title,
            href=page_hrefs[bookmark.page],
            file_href=page_hrefs[bookmark.page],
            anchor="",
            children=_bookmark_toc(bookmark.children, page_hrefs),
        )
        for bookmark in bookmarks
    ]


def _normalize_pdf_page_text(text: str) -> str:
    text = text.replace("\u00ad", "")
    lines = text.splitlines()
    lines = [
        line for line in lines
        if "© The Author(s)" not in line and "https://doi.org/" not in line
    ]
    if lines:
        first = lines[0].strip()
        if re.match(r"^\d+\s+\d+(?:\.\d+)*\.?(?:\s+[A-Z][A-Z .]+)?$", first):
            lines.pop(0)
            if lines and lines[0].strip().isupper():
                lines.pop(0)
        elif re.match(r"^\d+(?:\.\d+)+\.?\s+.+\s+\d+$", first):
            lines.pop(0)
        elif re.match(r"^.+\s+\d+$", first) and not first[:1].isdigit():
            lines.pop(0)
    text = "\n".join(lines)
    text = re.sub(r"(?<=\S)Chapter\s+\d+", "", text)
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def _extract_pdf_pages(reader: PdfReader) -> list[str]:
    raw_pages: list[str] = []
    for page in reader.pages:
        try:
            raw_pages.append(page.extract_text() or "")
        except Exception:
            raw_pages.append("")

    def signature(line: str) -> str:
        normalized = re.sub(r"\d+", "#", " ".join(line.lower().split()))
        return normalized if 1 < len(normalized) < 160 else ""

    edge_counts: Counter[str] = Counter()
    page_lines = []
    for text in raw_pages:
        lines = [line.strip() for line in text.splitlines()]
        page_lines.append(lines)
        candidates = lines[:2] + lines[-2:]
        edge_counts.update({signature(line) for line in candidates if signature(line)})
    repeat_threshold = max(5, len(raw_pages) // 20)
    repeated_edges = {key for key, count in edge_counts.items() if count >= repeat_threshold}

    pages = []
    for lines in page_lines:
        last_index = len(lines) - 1
        filtered = [
            line for index, line in enumerate(lines)
            if not ((index < 2 or index > last_index - 2) and signature(line) in repeated_edges)
        ]
        pages.append(_normalize_pdf_page_text("\n".join(filtered)))
    return pages


def _pdf_book(source: Path, output_dir: Path, source_url: str | None) -> Book:
    reader = PdfReader(str(source))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise DocumentImportError("Password-protected PDFs are not supported")
        except Exception as exc:
            if isinstance(exc, DocumentImportError):
                raise
            raise DocumentImportError("Password-protected PDFs are not supported") from exc

    page_count = len(reader.pages)
    if not page_count:
        raise DocumentImportError("PDF contains no pages")
    bookmarks = _pdf_outline(reader)
    flat_bookmarks = list(_flatten_bookmarks(bookmarks))
    page_titles: dict[int, str] = {}
    for bookmark in flat_bookmarks:
        page_titles.setdefault(bookmark.page, bookmark.title)

    start_pages = sorted(page_titles)
    if not start_pages:
        start_pages = list(range(1, page_count + 1, PDF_FALLBACK_SECTION_PAGES))
        page_titles = {
            page: (
                f"Pages {page}-{min(page + PDF_FALLBACK_SECTION_PAGES - 1, page_count)}"
                if page < page_count
                else f"Page {page}"
            )
            for page in start_pages
        }
    elif start_pages[0] > 1:
        page_titles[1] = "Front matter"
        start_pages.insert(0, 1)

    page_hrefs = {page: f"pdf-page-{page}" for page in start_pages}
    if bookmarks:
        toc = _bookmark_toc(bookmarks, page_hrefs)
        if start_pages[0] == 1 and not any(bookmark.page == 1 for bookmark in flat_bookmarks):
            toc.insert(0, TOCEntry(title="Front matter", href=page_hrefs[1], file_href=page_hrefs[1], anchor=""))
    else:
        toc = [
            TOCEntry(title=page_titles[page], href=page_hrefs[page], file_href=page_hrefs[page], anchor="")
            for page in start_pages
        ]

    extracted_pages = _extract_pdf_pages(reader)
    spine = []
    for index, start_page in enumerate(start_pages):
        end_page = start_pages[index + 1] - 1 if index + 1 < len(start_pages) else page_count
        page_parts = []
        for page_number in range(start_page, end_page + 1):
            page_text = extracted_pages[page_number - 1]
            if page_text:
                page_parts.append(f"[Page {page_number}]\n{page_text}")
        spine.append(ChapterContent(
            id=page_hrefs[start_page],
            href=page_hrefs[start_page],
            title=page_titles[start_page],
            content="",
            text="\n\n".join(page_parts),
            order=index,
            start_page=start_page,
            end_page=end_page,
        ))

    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_name = "document.pdf"
    shutil.copy2(source, assets_dir / asset_name)
    metadata = reader.metadata or {}
    title = str(metadata.get("/Title") or source.stem.replace("_", " ")).strip()
    author = str(metadata.get("/Author") or "").strip()
    return Book(
        metadata=BookMetadata(title=title or "Untitled", language="en", authors=[author] if author else []),
        spine=spine,
        toc=toc,
        images={},
        source_file=source.name,
        processed_at=datetime.now().isoformat(),
        document_type="pdf",
        asset_filename=asset_name,
        source_url=source_url,
        page_count=page_count,
    )


def _sanitize_html(raw_html: str, base_url: str | None = None) -> tuple[str, str, str]:
    soup = BeautifulSoup(raw_html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    for tag in soup(["script", "style", "iframe", "object", "embed", "form", "button", "input", "textarea", "select"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            if attribute.lower().startswith("on") or attribute.lower() in {"style", "srcdoc"}:
                del tag.attrs[attribute]

    root = soup.find("article") or soup.find("main") or soup.body or soup
    for tag in root.find_all(["nav", "aside"]):
        tag.decompose()

    if base_url:
        for tag, attribute in (("a", "href"), ("img", "src"), ("source", "src")):
            for node in root.find_all(tag):
                value = node.get(attribute)
                if value:
                    node[attribute] = urljoin(base_url, value)
        for node in root.find_all("img"):
            if node.get("srcset"):
                candidates = []
                for candidate in node["srcset"].split(","):
                    parts = candidate.strip().split()
                    if parts:
                        parts[0] = urljoin(base_url, parts[0])
                        candidates.append(" ".join(parts))
                node["srcset"] = ", ".join(candidates)

    for node in root.find_all(True):
        for attribute in ("href", "src"):
            value = node.get(attribute)
            if not value:
                continue
            parsed = urlparse(value.strip())
            allowed = {"", "http", "https", "mailto"} if attribute == "href" else {"", "http", "https", "data"}
            if parsed.scheme.lower() not in allowed:
                del node.attrs[attribute]

    content = "".join(str(node) for node in root.contents)
    text = " ".join(root.get_text(" ").split())
    if not title:
        heading = root.find(["h1", "h2"])
        title = heading.get_text(" ", strip=True) if heading else "HTML document"
    return title, content, text


def _html_book(source: Path, output_dir: Path, source_url: str | None) -> Book:
    raw_html = source.read_text(encoding="utf-8", errors="replace")
    title, content, text = _sanitize_html(raw_html, source_url)
    chapter = ChapterContent(
        id="document",
        href="document.html",
        title=title,
        content=content,
        text=text,
        order=0,
    )
    return Book(
        metadata=_metadata(title),
        spine=[chapter],
        toc=[TOCEntry(title=title, href="document.html", file_href="document.html", anchor="")],
        images={},
        source_file=source.name,
        processed_at=datetime.now().isoformat(),
        document_type="html" if not source_url else "url",
        source_url=source_url,
    )


def import_file(source_path: str | Path, output_root: str | Path = ".", source_url: str | None = None) -> str:
    source = Path(source_path)
    extension = source.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise DocumentImportError(f"Unsupported file type: {extension or 'unknown'}")

    output_root = Path(output_root)
    output_dir = _unique_output_dir(output_root, source.stem)
    try:
        if extension == ".epub":
            book = process_epub(str(source), str(output_dir))
            book.document_type = "epub"
            book.source_url = source_url
        elif extension == ".pdf":
            book = _pdf_book(source, output_dir, source_url)
        elif extension in SUPPORTED_IMAGE_EXTENSIONS:
            book = _asset_book(source, output_dir, "image", source_url)
        else:
            output_dir.mkdir(parents=True, exist_ok=False)
            book = _html_book(source, output_dir, source_url)
        save_to_pickle(book, str(output_dir))
    except Exception:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        raise
    return output_dir.name


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DocumentImportError("URL must start with http:// or https://")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise DocumentImportError("Could not resolve URL host") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise DocumentImportError("Local and private network URLs are not allowed")


def _extension_from_response(url: str, content_type: str) -> str:
    extension = Path(urlparse(url).path).suffix.lower()
    if extension in SUPPORTED_EXTENSIONS:
        return extension
    content_type = content_type.split(";", 1)[0].strip().lower()
    mapping = {
        "application/epub+zip": ".epub",
        "application/pdf": ".pdf",
        "text/html": ".html",
        "application/xhtml+xml": ".html",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/bmp": ".bmp",
    }
    guessed = mapping.get(content_type) or mimetypes.guess_extension(content_type)
    if guessed not in SUPPORTED_EXTENSIONS:
        raise DocumentImportError(f"Unsupported URL content type: {content_type or 'unknown'}")
    return guessed


def download_url(url: str, destination_dir: str | Path) -> tuple[Path, str]:
    current_url = url.strip()
    if not current_url:
        raise DocumentImportError("URL is required")
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=30, follow_redirects=False, headers={"User-Agent": "reader3/0.2"}) as client:
        for _ in range(6):
            _validate_public_url(current_url)
            with client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise DocumentImportError("URL redirect had no destination")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length", "")
                if content_length.isdigit() and int(content_length) > MAX_DOWNLOAD_BYTES:
                    raise DocumentImportError("URL download is larger than 100 MB")
                extension = _extension_from_response(current_url, response.headers.get("content-type", ""))
                name = _slugify(Path(urlparse(current_url).path).stem or urlparse(current_url).hostname or "download")
                destination = destination_dir / f"{name}{extension}"
                total = 0
                with destination.open("wb") as output:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise DocumentImportError("URL download is larger than 100 MB")
                        output.write(chunk)
                return destination, current_url
    raise DocumentImportError("Too many URL redirects")
