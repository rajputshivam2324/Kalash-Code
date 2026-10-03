---
name: presentations
description: Inspect slide text; create or edit PPTX presentations and render for layout checks.
version: 1.0.0
---

# presentations

Inspect selected PPTX slides with the artifact helper. Use python-pptx for semantic
editing; do not unzip/rebuild arbitrary Office packages to change text.
Load references/workflow.md for recipes. Reopen outputs to check slide count/content.
Render through an installed LibreOffice and Poppler workflow for layout verification.
Extracted slide text cannot verify overlaps, clipped content, theme fidelity or speaker notes.

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

