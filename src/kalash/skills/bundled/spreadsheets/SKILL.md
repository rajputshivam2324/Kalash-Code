---
name: spreadsheets
description: Inspect XLSX sheets and selected rows; create/edit XLSX, CSV and TSV with formulas.
version: 1.0.0
---

# spreadsheets

First inspect sheet names, then select a sheet and row range. Formula inspection returns
source, not recalculated values. Use openpyxl for XLSX and csv for CSV/TSV.
Load references/workflow.md for recipes. Preserve formulas, types and number formats;
reopen outputs to verify actual cells. openpyxl cannot recalculate formulas: use an
installed spreadsheet engine when calculated output is required. Legacy XLS needs conversion.

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

