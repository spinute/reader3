import os
import pickle
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from importers import MAX_DOWNLOAD_BYTES, DocumentImportError, download_url, import_file
from reader3 import Book, BookMetadata, ChapterContent, TOCEntry, format_section_context

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Where are the book folders located?
BOOKS_DIR = "."


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
    return {
        "document_title": book.metadata.title,
        "section_title": chapter.title,
        "start_page": getattr(chapter, "start_page", None),
        "end_page": getattr(chapter, "end_page", None),
        "character_count": len(chapter.text),
        "text": chapter.text,
        "context": context,
    }

@app.get("/read/{book_id}", response_class=HTMLResponse)
async def redirect_to_first_chapter(book_id: str):
    """Helper to just go to chapter 0."""
    return await read_chapter(book_id=book_id, chapter_index=0)


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

@app.get("/read/{book_id}/{chapter_index}", response_class=HTMLResponse)
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
