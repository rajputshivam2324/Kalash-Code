# XLSX recipes
```python
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

wb = Workbook()
ws = wb.active
ws.title = "Summary"
ws.append(["Item", "Amount"])
ws.append(["Alpha", 12])
ws.append(["Beta", 8])
ws["B4"] = "=SUM(B2:B3)"
ws["B4"].number_format = "0.00"
ws["A1"].font = Font(bold=True)
wb.save("summary.xlsx")
check = load_workbook("summary.xlsx", data_only=False)
assert check["Summary"]["B4"].value == "=SUM(B2:B3)"
check.close()
```
Inspect sheet names and selected rows:
```bash
python -m kalash.artifacts summary.xlsx --limit 5
python -m kalash.artifacts summary.xlsx --sheet Summary --offset 0 --limit 4
```
The inspector limits rows and columns (100 columns per row). Use a targeted script for
other columns. Prefer read_only/write_only streaming modes for large workbooks and always
close read-only workbooks. Cached values from data_only=True may be stale/absent.
Use an installed LibreOffice/Excel engine to recalculate when required and compare
independent expected aggregates. openpyxl alone cannot calculate formulas.
Macros/legacy XLS and complex Excel features need an explicit preservation/conversion plan.
CSV cells beginning with formula prefixes need a deliberate export policy when users
intend to open untrusted data in a spreadsheet application.
