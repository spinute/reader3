import os
import pickle
import re
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from importers import MAX_DOWNLOAD_BYTES, DocumentImportError, download_url, import_file
from llm_providers import LLMChatRequest, LLMProviderError, call_llm, provider_status
from reader3 import Book, format_section_context

app = FastAPI()
templates = Jinja2Templates(directory="templates")
DEV_MODE = os.getenv("READER3_DEV") == "1"
DEV_SERVER_VERSION = str(time.time_ns())
templates.env.globals.update(
    dev_mode=DEV_MODE,
    dev_server_version=DEV_SERVER_VERSION,
)

# Where are the book folders located?
BOOKS_DIR = "."
# Keep the complete handoff URL below the conservative 2 KB interoperability
# boundary. ChatGPT may reject longer GET requests with HTTP 431, especially
# once browser cookies and other request headers are included.
AI_URL_MAX_ENCODED_CHARS = 1800
AI_URL_TRUNCATION_NOTICE = (
    "\n\n[Reader 3 shortened this URL prompt to avoid a browser 431 error. "
    "Use the section copy icon or an API-backed provider for the complete section.]"
)
AI_PROVIDER_URLS = {
    "chatgpt": "https://chatgpt.com/?q=",
    "claude": "https://claude.ai/new?q=",
}
PROMPT_ACTIONS = {
    "read": "Read this section with me. Start by explaining its main idea, then invite me to ask questions. Cite source pages when available.",
    "explain": "Explain this section clearly. Define unfamiliar terms, walk through important reasoning step by step, and preserve references to source pages.",
    "summary": "Summarize this section. List the central claims, important definitions, and the minimum details needed to recall it later. Preserve references to source pages.",
    "quiz": "Tutor me on this section using retrieval practice. Ask one question at a time, wait for my answer, then give feedback and continue. Do not reveal all answers immediately.",
}


def encode_ai_url_prompt(prompt: str) -> str:
    """Encode a handoff prompt while bounding its actual URL-query size."""
    encoded = quote(prompt, safe="")
    if len(encoded) <= AI_URL_MAX_ENCODED_CHARS:
        return encoded

    low, high = 0, len(prompt)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = quote(prompt[:middle] + AI_URL_TRUNCATION_NOTICE, safe="")
        if len(candidate) <= AI_URL_MAX_ENCODED_CHARS:
            low = middle
        else:
            high = middle - 1
    return quote(prompt[:low] + AI_URL_TRUNCATION_NOTICE, safe="")


def list_books():
    """Return all imported documents for the library screen."""
    books = []
    if os.path.exists(BOOKS_DIR):
        for item in os.listdir(BOOKS_DIR):
            if item.endswith("_data") and os.path.isdir(item):
                book = load_book_cached(item)
                if book:
                    books.append(
                        {
                            "id": item,
                            "title": book.metadata.title,
                            "author": ", ".join(book.metadata.authors),
                            "chapters": len(book.spine),
                            "document_type": getattr(book, "document_type", "epub"),
                        }
                    )
    return sorted(books, key=lambda book: book["title"].lower())


@app.get("/__dev__/version", include_in_schema=False)
async def dev_server_version():
    """Expose the process version used by the development live-reload client."""
    if not DEV_MODE:
        raise HTTPException(status_code=404, detail="Development mode is disabled")
    return {"version": DEV_SERVER_VERSION}


@lru_cache(maxsize=10)
def load_book_cached(folder_name: str) -> Optional[Book]:
    """
    Loads the book from the pickle file.
    Cached so we don't re-read the disk on every click.
    """
    file_path = os.path.join(BOOKS_DIR, folder_name, "book.pkl")
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "rb") as f:
            book = pickle.load(f)
        return book
    except Exception as e:
        print(f"Error loading book {folder_name}: {e}")
        return None


@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """Lists all available processed books."""
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "request": request,
            "books": list_books(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/me", response_class=HTMLResponse)
async def my_page(request: Request):
    """Global browser-local AI provider and prompt settings."""
    return templates.TemplateResponse(request, "me.html", {"request": request})


def _redirect_with(kind: str, message: str) -> RedirectResponse:
    return RedirectResponse(url=f"/?{kind}={quote(message)}", status_code=303)


@app.post("/import/upload")
async def import_upload(file: UploadFile = File(...)):
    """Import a supported document uploaded from the library page."""
    filename = os.path.basename((file.filename or "document").replace("\\", "/"))
    try:
        with tempfile.TemporaryDirectory(prefix="reader3-upload-", dir=BOOKS_DIR) as temp_dir:
            destination = Path(temp_dir) / filename
            total = 0
            with destination.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise DocumentImportError("Upload is larger than 100 MB")
                    output.write(chunk)
            book_id = await run_in_threadpool(import_file, destination, BOOKS_DIR)
        load_book_cached.cache_clear()
        return RedirectResponse(url=f"/read/{book_id}/0", status_code=303)
    except DocumentImportError as exc:
        return _redirect_with("error", str(exc))
    except Exception as exc:
        print(f"Upload import failed: {exc}")
        return _redirect_with("error", "Could not import that file")
    finally:
        await file.close()


@app.post("/import/url")
async def import_url(url: str = Form(...)):
    """Download and import a public document or web page."""
    try:
        with tempfile.TemporaryDirectory(prefix="reader3-url-", dir=BOOKS_DIR) as temp_dir:
            downloaded, final_url = await run_in_threadpool(download_url, url, temp_dir)
            book_id = await run_in_threadpool(import_file, downloaded, BOOKS_DIR, final_url)
        load_book_cached.cache_clear()
        return RedirectResponse(url=f"/read/{book_id}/0", status_code=303)
    except DocumentImportError as exc:
        return _redirect_with("error", str(exc))
    except Exception as exc:
        print(f"URL import failed: {exc}")
        return _redirect_with("error", "Could not import that URL")


@app.get("/api/read/{book_id}/{chapter_index}/context")
async def section_context(book_id: str, chapter_index: int):
    """Return portable LLM context for one document section."""
    book = load_book_cached(book_id)
    if not book or chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="Section not found")
    chapter = book.spine[chapter_index]
    context = format_section_context(book, chapter)
    media = []
    for media_path in getattr(chapter, "media", []):
        match = re.search(r"pdf-page-(\d+)-image-", media_path)
        caption = getattr(chapter, "media_captions", {}).get(media_path)
        media.append(
            {
                "url": f"/read/{book_id}/images/{os.path.basename(media_path)}",
                "page": int(match.group(1)) if match else None,
                "alt": caption
                or (
                    f"Extracted figure from page {match.group(1)}" if match else "Extracted figure"
                ),
            }
        )
    markdown = re.sub(r"^\[Page (\d+)\]$", r"_Page \1_", chapter.text, flags=re.MULTILINE)
    source_bits = [book.metadata.title]
    if getattr(chapter, "start_page", None):
        page_range = str(chapter.start_page)
        if chapter.end_page and chapter.end_page != chapter.start_page:
            page_range += f"-{chapter.end_page}"
        source_bits.append(f"source pages {page_range}")
    markdown = f"# {chapter.title}\n\n> {' · '.join(source_bits)}\n\n{markdown}".strip()
    if media:
        markdown += "\n\n## Extracted images\n\n" + "\n\n".join(
            f"![{item['alt']}]({item['url']})" for item in media
        )
    return {
        "document_title": book.metadata.title,
        "section_title": chapter.title,
        "start_page": getattr(chapter, "start_page", None),
        "end_page": getattr(chapter, "end_page", None),
        "character_count": len(chapter.text),
        "text": chapter.text,
        "markdown": markdown,
        "media": media,
        "context": context,
    }


@app.get("/api/llm/status")
async def llm_status():
    """Report local provider availability without exposing credentials."""
    return provider_status()


@app.post("/api/llm/chat")
async def llm_chat(payload: LLMChatRequest):
    """Call the selected API/local model; tokens are used for this request only."""
    try:
        response = await call_llm(payload)
        return {"message": response, "provider": payload.provider, "model": payload.model}
    except LLMProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        print(f"LLM request failed: {type(exc).__name__}")
        raise HTTPException(status_code=500, detail="The LLM request failed") from exc


@app.get("/open/{provider}/{book_id}/{chapter_index}")
async def open_ai_chat(
    provider: str,
    book_id: str,
    chapter_index: int,
    action: str = "read",
    instruction: str = "",
    preferences: str = "",
):
    """Open a supported AI chat from a normal browser link (popup-safe)."""
    provider_url = AI_PROVIDER_URLS.get(provider)
    book = load_book_cached(book_id)
    if not provider_url or not book or chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="AI handoff not found")
    chapter = book.spine[chapter_index]
    selected_instruction = instruction.strip()[:2_000] or PROMPT_ACTIONS.get(
        action, PROMPT_ACTIONS["read"]
    )
    response_preferences = preferences.strip()[:4_000]
    if response_preferences:
        selected_instruction += f"\n\nUser response preferences:\n{response_preferences}"
    full_prompt = f"{selected_instruction}\n\n{format_section_context(book, chapter)}"
    return RedirectResponse(provider_url + encode_ai_url_prompt(full_prompt), status_code=303)


@app.get("/read/{book_id}", response_class=HTMLResponse)
async def redirect_to_first_chapter(request: Request, book_id: str):
    """Helper to just go to chapter 0."""
    return await read_chapter(request=request, book_id=book_id, chapter_index=0)


@app.get("/read/{book_id}/asset")
async def serve_asset(book_id: str):
    """Serve an imported PDF or image without modifying the original bytes."""
    if book_id != os.path.basename(book_id):
        raise HTTPException(status_code=404, detail="Document not found")
    book = load_book_cached(book_id)
    asset_filename = getattr(book, "asset_filename", None) if book else None
    if not asset_filename:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset_path = Path(BOOKS_DIR) / book_id / "assets" / os.path.basename(asset_filename)
    if not asset_path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    media_type = "application/pdf" if getattr(book, "document_type", "") == "pdf" else None
    return FileResponse(asset_path, media_type=media_type, content_disposition_type="inline")


@app.get("/read/{book_id}/{chapter_index:int}", response_class=HTMLResponse)
async def read_chapter(request: Request, book_id: str, chapter_index: int):
    """The main reader interface."""
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    if chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="Chapter not found")

    current_chapter = book.spine[chapter_index]

    # Calculate Prev/Next links
    prev_idx = chapter_index - 1 if chapter_index > 0 else None
    next_idx = chapter_index + 1 if chapter_index < len(book.spine) - 1 else None

    return templates.TemplateResponse(
        request,
        "reader.html",
        {
            "request": request,
            "book": book,
            "current_chapter": current_chapter,
            "chapter_index": chapter_index,
            "book_id": book_id,
            "prev_idx": prev_idx,
            "next_idx": next_idx,
            "document_type": getattr(book, "document_type", "epub"),
        },
    )


@app.get("/read/{book_id}/images/{image_name}")
async def serve_image(book_id: str, image_name: str):
    """
    Serves images specifically for a book.
    The HTML contains <img src="images/pic.jpg">.
    The browser resolves this to /read/{book_id}/images/pic.jpg.
    """
    # Security check: ensure book_id is clean
    safe_book_id = os.path.basename(book_id)
    safe_image_name = os.path.basename(image_name)

    img_path = os.path.join(BOOKS_DIR, safe_book_id, "images", safe_image_name)

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)


if __name__ == "__main__":
    import uvicorn

    print("Starting server at http://127.0.0.1:8123")
    uvicorn.run(app, host="127.0.0.1", port=8123)
