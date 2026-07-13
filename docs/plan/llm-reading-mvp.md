# LLM-assisted reading MVP

## Goal

Make every reader3 document useful for Karpathy-style reading: preserve the source for reading, expose meaningful sections, and make the current section easy to send to any LLM with enough source context to discuss it accurately.

## Current gaps

- PDF input is a single embedded asset with no reader3 table of contents.
- PDF text and page ranges are not extracted.
- The reader has no section-copy or prompt-copy actions.
- Image documents have no convenient path to a multimodal LLM.
- Existing EPUB/HTML section text is stored, but the UI does not expose it.

## Scope

### 1. Common section model

Extend each `ChapterContent` with optional source-location metadata:

- `start_page` and `end_page` for PDF sections.
- A stable section ID used by the table of contents and routes.
- Existing cleaned `text` as the canonical LLM context.

Keep old pickle files readable by using optional fields with defaults and `getattr` at compatibility boundaries.

### 2. PDF import

- Keep the original PDF bytes and render them without conversion.
- Read PDF metadata, outline/bookmarks, and page text with `pypdf`.
- Convert the outline into reader3's nested TOC.
- Build ordered, non-overlapping sections from unique outline destinations.
- Associate each section with a start page, end page, title, and extracted text.
- If the PDF has no usable outline, create page-range sections so it remains navigable and copyable.
- Normalize extracted text by removing repeated headers/footers, joining line-break hyphenation, and collapsing layout whitespace conservatively.

### 3. Reading and LLM actions

- Navigate PDF sections from the reader3 sidebar and open the PDF at the section's first page.
- Display the current PDF page range and extracted character count.
- Add actions for:
  - copying the current section with title and source location;
  - copying the user's current text selection;
  - copying an explanation prompt plus the section;
  - copying a summary prompt plus the section;
  - copying a comprehension-quiz prompt plus the section;
  - copying an image document for a multimodal LLM.
- Keep provider integration out of the MVP: clipboard output works with ChatGPT, Claude, local models, and future providers without credentials.

### 4. Safety and limits

- Continue sanitizing imported HTML.
- Continue rejecting private/local URL targets and downloads above 100 MB.
- Never execute content from imported documents.
- Treat extracted PDF text as supporting context; the original rendered PDF remains the visual source of truth.

## Acceptance criteria

- A PDF with bookmarks shows a reader3 TOC with multiple navigable sections.
- Clicking a PDF TOC entry opens the corresponding source page.
- Each PDF section has non-empty extracted text when the source page contains extractable text.
- Copy actions include document title, section title, and PDF page range.
- EPUB and HTML sections support the same copy/prompt actions.
- Image documents expose a copy-image action.
- Existing EPUB data remains readable.
- Automated tests cover outline conversion, PDF fallback sections, context formatting, upload routes, sanitization, and URL restrictions.
- The Bishop PDF is re-imported and verified to retain its original 47 MB PDF while exposing its TOC and section text.

## Deferred work

- Configurable OCR languages and extracting text from standalone images (English Tesseract fallback is implemented for image-only PDF pages).
- Equation-to-LaTeX conversion.
- Semantic figure/caption association and multimodal section bundles (raster figures are already extracted by page).
- Authenticated provider APIs for ChatGPT, Claude, Apple Foundation Models, or Ollama.
- Search, notes, highlights, reading-position sync, and retrieval across multiple sections.

## Follow-up implementation

The browser handoff and reading-performance follow-up adds:

- in-page PDF section navigation without a full reader-page reload;
- a fast Markdown-style mode alongside the original PDF, with page headings and extracted raster figures;
- lazy section-text loading and caching;
- synchronous URL-query handoff links for ChatGPT and Claude, avoiding popup-blocker failures;
- an Ask Gemini action that prepares the current tab and displays Chrome's `Control-G` shortcut;
- a 7,800-character URL limit with explicit truncation feedback.

Direct provider APIs, authenticated conversations, configurable OCR, equation-to-LaTeX conversion, and automatic Gemini in Chrome side-panel activation remain deferred. Gemini in Chrome can read the current tab through Chrome's own UI, but regular web pages do not have a public API for opening that privileged side panel.
