# reader 3

![reader3](reader3.png)

A lightweight, self-hosted document reader for reading EPUB books and web documents one section at a time. It makes content easy to copy into an LLM so you can read along together. PDFs are displayed without conversion, preserving their original typography, equations, and figures.

Supported inputs:

- EPUB
- PDF
- HTML
- PNG, JPEG, GIF, WebP, BMP, and AVIF images
- Public HTTP/HTTPS URLs containing any of the formats above

This project was 90% vibe coded just to illustrate how one can very easily [read books together with LLMs](https://x.com/karpathy/status/1990577951671509438). I'm not going to support it in any way, it's provided here as is for other people's inspiration and I don't intend to improve it. Code is ephemeral now and libraries are over, ask your LLM to change it in whatever way you like.

## Usage

The project uses [uv](https://docs.astral.sh/uv/). Start the server:

```bash
uv run server.py
```

Visit [localhost:8123](http://localhost:8123/), then upload a document or paste a public URL. Files and URL downloads are limited to 100 MB. Private and local network URLs are rejected.

## Reading with an LLM

Reader 3 keeps the source document visible while preparing each section as portable LLM context:

- PDF bookmarks become a nested table of contents with page-range navigation.
- Multiple PDF outline headings on the same page remain separate Markdown sections.
- Extractable PDF text is stored by section while the original PDF remains unchanged.
- Image-only PDF pages use local Tesseract OCR when it is installed; imports still work without it.
- Extracted raster figures use visible `Figure` captions when the PDF text contains them.
- EPUB and HTML content use their existing section text.
- **Copy Markdown** is the single full-section copy action; it includes the document title, section title, source pages, text, and extracted-image links.
- Section titles, TOC entries, and individual paragraphs have nearby copy buttons.
- **Explain**, **Summarize**, **Quiz me**, and user-defined prompt buttons run with the LLM selected under **⚙ AI settings**.
- **Copy selection** handles shorter excerpts from EPUB and HTML documents.
- **Copy image** prepares standalone images for a multimodal LLM.
- PDF headings navigate without reloading the whole reader page.
- The in-page **PDF / Markdown** control switches between the original layout and heading-scoped extracted text with page labels and available raster figures.
- Web handoff supports **ChatGPT**, **Claude**, and **Ask Gemini in Chrome**.
- API-backed chat supports OpenAI Responses, Anthropic Messages, OpenAI-compatible endpoints such as Ollama, and Apple's on-device Foundation Model SDK when the host supports it.
- The right-side AI chat panel saves multiple chat histories in browser local storage.

URL handoff is capped at 7,800 characters for browser compatibility. Chrome does not expose its privileged Ask Gemini panel to ordinary page JavaScript, so reader3 prepares and copies the prompt; press `Control-G` and paste it into Ask Gemini.

API tokens are stored only in the current browser's local storage and are sent through the localhost reader3 server for the selected provider request. They are not written to the document library. Apple Foundation Models currently require a supported Apple Silicon Mac and a compatible macOS/SDK; unavailable hosts show the reason in settings.

OCR quality depends on the scan and currently defaults to English. See [`docs/plan/llm-reading-mvp.md`](docs/plan/llm-reading-mvp.md) for the implemented scope and deferred work.

You can still import EPUB files from the command line. For example, download [Dracula EPUB3](https://www.gutenberg.org/ebooks/345) as `dracula.epub`, then:

```bash
uv run reader3.py dracula.epub
```

This creates `dracula_data` and registers the book in the local library. Delete a document's `_data` directory to remove it.

## License

MIT
