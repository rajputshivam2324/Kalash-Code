---
name: archive
description: List ZIP/TAR members and create or safely extract archives without path traversal.
version: 1.0.0
---

# archive

List archive members with the artifact helper before extraction. Listing never extracts.
Load references/workflow.md for safe operations. Choose a dedicated destination inside the
workspace. Reject absolute paths, traversal, links, device entries and oversized members.
Never blindly call extractall on untrusted ZIP data or use a permissive TAR filter.
Apply size/member limits even to decompressed data; inspect outputs after extraction.

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

