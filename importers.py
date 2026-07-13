"""Import EPUB, PDF, image, HTML, and web documents into reader3."""

from __future__ import annotations

import ipaddress
import mimetypes
import re
import shutil
import socket
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Comment

from reader3 import (
    Book,
    BookMetadata,
    ChapterContent,
    TOCEntry,
    process_epub,
    save_to_pickle,
)


MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
SUPPORTED_EXTENSIONS = {".epub", ".pdf", ".html", ".htm", *SUPPORTED_IMAGE_EXTENSIONS}


class DocumentImportError(ValueError):
    pass


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
            book = _asset_book(source, output_dir, "pdf", source_url)
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
