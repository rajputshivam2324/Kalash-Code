---
name: documents
description: Read, create and edit DOCX, Markdown and text; convert RTF with an installed converter.
version: 1.0.0
---

# documents

Inspect selected DOCX paragraphs (including table text) with the artifact helper.
Use python-docx for semantic document editing and native read/edit/write for plain text.
Load references/workflow.md for creation and conversion recipes. Preserve the source and
write a separate output unless overwriting is authorized. LibreOffice is optional for
RTF conversion and DOCX-to-PDF rendering. Text extraction does not prove layout quality.

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

