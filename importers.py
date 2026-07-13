"""Import EPUB, PDF, image, HTML, and web documents into reader3."""

from __future__ import annotations

import ipaddress
import io
import mimetypes
import re
import shutil
import socket
import subprocess
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
PDF_MAX_IMAGES_PER_PAGE = 16
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


def _bookmark_toc(bookmarks: list[_PdfBookmark]) -> list[TOCEntry]:
    return [
        TOCEntry(
            title=bookmark.title,
            href=bookmark.href,
            file_href=bookmark.href,
            anchor="",
            children=_bookmark_toc(bookmark.children),
        )
        for bookmark in bookmarks
    ]


def _phrase_span(text: str, phrase: str, start: int = 0):
    tokens = re.findall(r"[^\W_]+", phrase, flags=re.UNICODE)
    if not tokens:
        return None
    pattern = r"(?<!\w)" + r"[\W_]+".join(re.escape(token) for token in tokens) + r"(?!\w)"
    return re.search(pattern, text[start:], flags=re.IGNORECASE)


def _section_page_ranges(entries: list[_PdfBookmark], page_count: int) -> list[tuple[int, int]]:
    ranges = []
    for index, entry in enumerate(entries):
        next_page = entries[index + 1].page if index + 1 < len(entries) else page_count + 1
        end_page = entry.page if next_page <= entry.page else next_page - 1
        ranges.append((entry.page, min(end_page, page_count)))
    return ranges


def _split_pdf_section_texts(
    extracted_pages: list[str], entries: list[_PdfBookmark], page_count: int
) -> tuple[list[str], list[tuple[int, int]]]:
    """Split outline entries on the same page at their visible heading text."""
    ranges = _section_page_ranges(entries, page_count)
    section_texts = []
    for index, entry in enumerate(entries):
        start_page, end_page = ranges[index]
        next_entry = entries[index + 1] if index + 1 < len(entries) else None
        page_parts = []
        for page_number in range(start_page, end_page + 1):
            page_text = extracted_pages[page_number - 1]
            if page_number == start_page and entry.title != "Front matter":
                heading = _phrase_span(page_text, entry.title)
                content_start = heading.end() if heading else 0
                page_text = page_text[content_start:].lstrip(" .:–—-\n")
                if next_entry and next_entry.page == start_page:
                    next_heading = _phrase_span(page_text, next_entry.title)
                    if next_heading:
                        page_text = page_text[: next_heading.start()].rstrip()
                    elif not heading:
                        page_text = ""
            if page_text:
                page_parts.append(f"[Page {page_number}]\n{page_text}")
        section_texts.append("\n\n".join(page_parts))
    return section_texts, ranges


def _normalize_pdf_page_text(text: str) -> str:
    text = text.replace("\u00ad", "")
    lines = text.splitlines()
    lines = [
        line for line in lines if "© The Author(s)" not in line and "https://doi.org/" not in line
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


def _ocr_pdf_page(page) -> str:
    """OCR a likely scanned page when Tesseract and a large page image are available."""
    executable = shutil.which("tesseract")
    if not executable:
        return ""
    try:
        candidates = list(page.images)
        if not candidates:
            return ""
        image_file = max(
            candidates, key=lambda candidate: candidate.image.width * candidate.image.height
        )
        image = image_file.image
        if image.width * image.height < 250_000:
            return ""
        payload = io.BytesIO()
        image.convert("RGB").save(payload, format="PNG")
        result = subprocess.run(
            [executable, "stdin", "stdout", "-l", "eng", "--dpi", "300"],
            input=payload.getvalue(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
        return result.stdout.decode("utf-8", errors="replace") if result.returncode == 0 else ""
    except Exception:
        return ""


def _extract_pdf_pages(reader: PdfReader) -> list[str]:
    raw_pages: list[str] = []
    for page in reader.pages:
        try:
            raw_text = page.extract_text() or ""
        except Exception:
            raw_text = ""
        if len(re.sub(r"\s+", "", raw_text)) < 20:
            raw_text = _ocr_pdf_page(page) or raw_text
        raw_pages.append(raw_text)

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
            line
            for index, line in enumerate(lines)
            if not ((index < 2 or index > last_index - 2) and signature(line) in repeated_edges)
        ]
        pages.append(_normalize_pdf_page_text("\n".join(filtered)))
    return pages


def _extract_pdf_captions(reader: PdfReader) -> dict[int, list[str]]:
    """Extract visible Figure/Fig. caption lines for accessible image labels."""
    result: dict[int, list[str]] = {}
    caption_pattern = re.compile(r"^(?:Figure|Fig\.)\s+\d+(?:\.\d+)*\s+.+", re.IGNORECASE)
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            lines = [" ".join(line.split()) for line in (page.extract_text() or "").splitlines()]
        except Exception:
            continue
        captions = []
        for index, line in enumerate(lines):
            if not caption_pattern.match(line):
                continue
            caption = line
            cursor = index + 1
            while cursor < len(lines) and len(caption) < 320 and not re.search(r"[.!?]$", caption):
                continuation = lines[cursor]
                if not continuation or caption_pattern.match(continuation):
                    break
                caption = (
                    caption[:-1] + continuation
                    if caption.endswith("-")
                    else f"{caption} {continuation}"
                )
                cursor += 1
            captions.append(caption[:320])
        if captions:
            result[page_number] = captions
    return result


def _extract_pdf_images(reader: PdfReader, output_dir: Path) -> dict[int, list[str]]:
    """Extract useful raster figures while ignoring tiny PDF drawing fragments."""
    images_dir = output_dir / "images"
    page_images: dict[int, list[str]] = {}
    for page_number, page in enumerate(reader.pages, start=1):
        extracted = []
        try:
            candidates = page.images
        except Exception:
            continue
        for image_file in candidates:
            try:
                width, height = image_file.image.size
                if (
                    width < 120
                    or height < 120
                    or width * height < 25_000
                    or len(image_file.data) < 5_000
                ):
                    continue
                extension = Path(image_file.name).suffix.lower()
                if extension not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                    extension = ".png"
                filename = f"pdf-page-{page_number}-image-{len(extracted) + 1}{extension}"
                images_dir.mkdir(parents=True, exist_ok=True)
                destination = images_dir / filename
                if extension == Path(image_file.name).suffix.lower():
                    destination.write_bytes(image_file.data)
                else:
                    image_file.image.save(destination, format="PNG")
                extracted.append(f"images/{filename}")
                if len(extracted) >= PDF_MAX_IMAGES_PER_PAGE:
                    break
            except Exception:
                continue
        if extracted:
            page_images[page_number] = extracted
    return page_images


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
    if flat_bookmarks:
        entries = [
            item[1]
            for item in sorted(enumerate(flat_bookmarks), key=lambda item: (item[1].page, item[0]))
        ]
        toc = _bookmark_toc(bookmarks)
        if entries[0].page > 1:
            front_matter = _PdfBookmark(title="Front matter", page=1, href="pdf-front-matter")
            entries.insert(0, front_matter)
            toc.insert(
                0,
                TOCEntry(
                    title=front_matter.title,
                    href=front_matter.href,
                    file_href=front_matter.href,
                    anchor="",
                ),
            )
    else:
        start_pages = list(range(1, page_count + 1, PDF_FALLBACK_SECTION_PAGES))
        entries = [
            _PdfBookmark(
                page=page,
                href=f"pdf-page-{page}",
                title=(
                    f"Pages {page}-{min(page + PDF_FALLBACK_SECTION_PAGES - 1, page_count)}"
                    if page < page_count
                    else f"Page {page}"
                ),
            )
            for page in start_pages
        ]
        toc = [
            TOCEntry(title=entry.title, href=entry.href, file_href=entry.href, anchor="")
            for entry in entries
        ]

    extracted_pages = _extract_pdf_pages(reader)
    extracted_images = _extract_pdf_images(reader, output_dir)
    extracted_captions = _extract_pdf_captions(reader)
    section_texts, page_ranges = _split_pdf_section_texts(extracted_pages, entries, page_count)
    section_media: list[list[str]] = [[] for _ in entries]
    section_captions: list[dict[str, str]] = [{} for _ in entries]
    for page_number, image_paths in extracted_images.items():
        candidates = [
            index
            for index, (start_page, end_page) in enumerate(page_ranges)
            if start_page <= page_number <= end_page
        ]
        if not candidates:
            continue
        captions = extracted_captions.get(page_number, [])
        for image_index, image_path in enumerate(image_paths):
            caption = captions[min(image_index, len(captions) - 1)] if captions else ""
            caption_hint = " ".join(caption.split()[:8])
            matching = [
                index
                for index in candidates
                if caption_hint and _phrase_span(section_texts[index], caption_hint)
            ]
            target = matching[0] if matching else candidates[-1]
            section_media[target].append(image_path)
            if caption:
                section_captions[target][image_path] = caption

    spine = []
    for index, entry in enumerate(entries):
        start_page, end_page = page_ranges[index]
        spine.append(
            ChapterContent(
                id=entry.href,
                href=entry.href,
                title=entry.title,
                content="",
                text=section_texts[index],
                order=index,
                start_page=start_page,
                end_page=end_page,
                media=section_media[index],
                media_captions=section_captions[index],
            )
        )

    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_name = "document.pdf"
    shutil.copy2(source, assets_dir / asset_name)
    metadata = reader.metadata or {}
    title = str(metadata.get("/Title") or source.stem.replace("_", " ")).strip()
    author = str(metadata.get("/Author") or "").strip()
    return Book(
        metadata=BookMetadata(
            title=title or "Untitled", language="en", authors=[author] if author else []
        ),
        spine=spine,
        toc=toc,
        images={
            image_path: image_path for paths in extracted_images.values() for image_path in paths
        },
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

    for tag in soup(
        [
            "script",
            "style",
            "iframe",
            "object",
            "embed",
            "form",
            "button",
            "input",
            "textarea",
            "select",
        ]
    ):
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
            allowed = (
                {"", "http", "https", "mailto"}
                if attribute == "href"
                else {"", "http", "https", "data"}
            )
            if parsed.scheme.lower() not in allowed:
                del node.attrs[attribute]

    content = "".join(str(node) for node in root.contents)
    text_root = BeautifulSoup(str(root), "html.parser")
    for br in text_root.find_all("br"):
        br.replace_with("\n")
    for block in text_root.find_all(
        [
            "article",
            "section",
            "div",
            "p",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "blockquote",
            "pre",
            "tr",
        ]
    ):
        block.insert_before("\n\n")
        block.append("\n\n")
    text = text_root.get_text()
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{2,}", "\n\n", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text).strip()
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


def import_file(
    source_path: str | Path, output_root: str | Path = ".", source_url: str | None = None
) -> str:
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
        addresses = socket.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
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

    with httpx.Client(
        timeout=30, follow_redirects=False, headers={"User-Agent": "reader3/0.2"}
    ) as client:
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
                extension = _extension_from_response(
                    current_url, response.headers.get("content-type", "")
                )
                name = _slugify(
                    Path(urlparse(current_url).path).stem
                    or urlparse(current_url).hostname
                    or "download"
                )
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
