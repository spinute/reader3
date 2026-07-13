import os
import pickle
import platform
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from importers import MAX_DOWNLOAD_BYTES, DocumentImportError, download_url, import_file
from llm_providers import LLMChatRequest, LLMProviderError, call_llm, provider_status
from reader3 import Book, BookMetadata, ChapterContent, TOCEntry, format_section_context

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Where are the book folders located?
BOOKS_DIR = "."
AI_URL_MAX_ENCODED_CHARS = 6000
AI_URL_TRUNCATION_NOTICE = (
    "\n\n[Reader 3 shortened this URL prompt to avoid a browser 431 error. "
    "Use Copy Markdown or an API-backed provider for the complete section.]"
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


class GeminiChromeRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=120_000)


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


def run_gemini_chrome(prompt: str) -> None:
    """Open Chrome's Ask Gemini panel, paste the prompt, and submit it on macOS."""
    if platform.system() != "Darwin":
        raise RuntimeError("Ask Gemini automation is available only on macOS")
    script = r'''
on run argv
    set promptText to item 1 of argv
    set the clipboard to promptText
    tell application "Google Chrome" to activate
    delay 0.1
    tell application "System Events"
        set frontmost of process "Google Chrome" to true
        key code 5 using {control down}
        set promptReady to false
        repeat 50 times
            delay 0.1
            try
                set focusedElement to value of attribute "AXFocusedUIElement" of process "Google Chrome"
                set focusedRole to value of attribute "AXRole" of focusedElement
                if focusedRole is in {"AXTextArea", "AXTextField", "AXComboBox"} then
                    set promptReady to true
                    exit repeat
                end if
            end try
        end repeat
        if promptReady is false then
            error "Ask Gemini opened, but its prompt field did not receive focus."
        end if
        set promptInserted to false
        try
            set value of attribute "AXValue" of focusedElement to promptText
            delay 0.1
            set insertedValue to value of attribute "AXValue" of focusedElement as text
            if (count characters of insertedValue) > 0 then set promptInserted to true
        end try
        if promptInserted is false then
            key code 0 using {command down}
            key code 9 using {command down}
            delay 0.25
            try
                set insertedValue to value of attribute "AXValue" of focusedElement as text
                if (count characters of insertedValue) > 0 then set promptInserted to true
            end try
        end if
        if promptInserted is false then
            error "Ask Gemini opened, but reader3 could not insert the prompt. The prompt remains on the clipboard."
        end if
        key code 36
    end tell
    return "sent " & (count characters of insertedValue)
end run
'''
    completed = subprocess.run(
        ["osascript", "-e", script, prompt],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        if "not allowed to send keystrokes" in detail:
            raise RuntimeError(
                "macOS blocked Chrome keyboard control. Enable Accessibility for the app "
                "running reader3 in System Settings > Privacy & Security > Accessibility, "
                "then retry. The prompt is already on the clipboard."
            )
        raise RuntimeError(detail or f"Chrome automation exited with status {completed.returncode}")


def list_books():
    """Return all imported documents for the library screen."""
    books = []
    if os.path.exists(BOOKS_DIR):
        for item in os.listdir(BOOKS_DIR):
            if item.endswith("_data") and os.path.isdir(item):
                book = load_book_cached(item)
                if book:
                    books.append({
                        "id": item,
                        "title": book.metadata.title,
                        "author": ", ".join(book.metadata.authors),
                        "chapters": len(book.spine),
                        "document_type": getattr(book, "document_type", "epub"),
                    })
    return sorted(books, key=lambda book: book["title"].lower())

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
    return templates.TemplateResponse(request, "library.html", {
        "request": request,
        "books": list_books(),
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    })


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
        media.append({
            "url": f"/read/{book_id}/images/{os.path.basename(media_path)}",
            "page": int(match.group(1)) if match else None,
            "alt": caption or (f"Extracted figure from page {match.group(1)}" if match else "Extracted figure"),
        })
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


@app.post("/api/gemini/chrome")
async def ask_gemini_in_chrome(payload: GeminiChromeRequest):
    """Invoke Chrome's native Ask Gemini UI from the local macOS reader."""
    try:
        await run_in_threadpool(run_gemini_chrome, payload.prompt)
        return {"opened": True}
    except (RuntimeError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/open/{provider}/{book_id}/{chapter_index}")
async def open_ai_chat(
    provider: str,
    book_id: str,
    chapter_index: int,
    action: str = "read",
    instruction: str = "",
):
    """Open a supported AI chat from a normal browser link (popup-safe)."""
    provider_url = AI_PROVIDER_URLS.get(provider)
    book = load_book_cached(book_id)
    if not provider_url or not book or chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="AI handoff not found")
    chapter = book.spine[chapter_index]
    selected_instruction = instruction.strip()[:2_000] or PROMPT_ACTIONS.get(action, PROMPT_ACTIONS["read"])
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

    return templates.TemplateResponse(request, "reader.html", {
        "request": request,
        "book": book,
        "current_chapter": current_chapter,
        "chapter_index": chapter_index,
        "book_id": book_id,
        "prev_idx": prev_idx,
        "next_idx": next_idx,
        "document_type": getattr(book, "document_type", "epub"),
    })

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
