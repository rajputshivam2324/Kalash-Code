---
name: pdf
description: Extract selected PDF pages, create or modify PDFs, and render for layout verification.
version: 1.0.0
---

# pdf

Inspect selected pages with the artifact helper; pypdf reads text but performs no OCR.
Use pypdf for page operations and reportlab for new PDFs. Load references/workflow.md
for concrete recipes. For scanned pages use an installed OCR workflow; do not assume blank
text means a blank page. Render with Poppler when layout matters and inspect the render
through an available vision integration. Report if visual verification is unavailable.

Run artifact inspection through the existing shell tool:
```bash
python -m kalash.artifacts "input.ext" --offset 0 --limit 10 --max-chars 8000
```
Use the Python environment where Kalash is installed (often `uv run python`).
The output includes path, SHA-256, unit labels, continuation offset and truncation flags.
Optional parsers are installed with `uv sync --extra artifacts` in a checkout or
`python -m pip install 'kalash-code[artifacts]'` in an installed environment.
Check dependencies in the execution environment; never install implicitly on skill load.
All scripts and conversions execute via shell under the normal permissions and timeout.

