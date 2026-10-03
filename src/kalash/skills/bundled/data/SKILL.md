---
name: data
description: Inspect and process JSON, JSONL, CSV and TSV with bounded output and preserved types.
version: 1.0.0
---

# data

Inspect selected top-level JSON entries or rows with the artifact helper. Use streaming
csv/JSONL processing for large data, with explicit schema, encoding and delimiter.
Load references/workflow.md for recipes. Preserve identifiers, leading zeros, date/time
semantics and nulls. Never use eval on data. Validate output by reopening, checking row
counts/types and comparing independent aggregates; avoid dumping datasets into context.

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

