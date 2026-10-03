# DOCX and conversions
```python
from docx import Document

doc = Document()
doc.add_heading("Project report", 0)
doc.add_paragraph("Verified results and next steps.")
table = doc.add_table(rows=1, cols=2)
table.rows[0].cells[0].text = "Check"
table.rows[0].cells[1].text = "Result"
doc.save("report.docx")
loaded = Document("report.docx")
assert loaded.paragraphs[0].text == "Project report"
assert len(loaded.tables) == 1
```
For existing documents, preserve runs/styles; assigning paragraph.text destroys run-level
formatting. Inspect tables, sections, headers/footers and relationships before editing.
Create an output directory, then use an installed LibreOffice converter:
```bash
libreoffice -env:UserInstallation=file:///tmp/kalash-doc-render --headless --convert-to pdf --outdir previews report.docx
```
Use a task-specific temporary profile and avoid concurrent reuse. RTF can be converted
to DOCX with the same converter. Reopen the result; rendering and vision review are needed
for layout claims. Do not claim round-trip fidelity for features the library cannot preserve.
