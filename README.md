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

You can still import EPUB files from the command line. For example, download [Dracula EPUB3](https://www.gutenberg.org/ebooks/345) as `dracula.epub`, then:

```bash
uv run reader3.py dracula.epub
```

This creates `dracula_data` and registers the book in the local library. Delete a document's `_data` directory to remove it.

## License

MIT
