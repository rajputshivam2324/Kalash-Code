---
name: images
description: Inspect image metadata and transform raster images; use connected vision for pixel understanding.
version: 1.0.0
---

# images

The artifact helper reports dimensions/format/mode, not visual understanding.
Use Pillow for crop, resize, format conversion and compositing. Load references/workflow.md.
Preserve transparency and aspect ratio; save a separate output and reopen it to validate.
For visual descriptions or rendered-artifact review, use a connected vision-capable tool
found with tool_search. Do not claim to have viewed pixels from metadata. Image generation
requires a separate configured model/tool and is not implied by Pillow support.

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

