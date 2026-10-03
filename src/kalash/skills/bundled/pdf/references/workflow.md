# PDF operations

Create a separate output with ReportLab, then reopen it with pypdf:
```python
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter

c = canvas.Canvas("report.pdf")
c.drawString(72, 750, "Report title")
c.showPage()
c.save()
reader = PdfReader("report.pdf")
assert len(reader.pages) == 1
assert "Report title" in reader.pages[0].extract_text()
writer = PdfWriter()
writer.add_page(reader.pages[0])
with open("selected.pdf", "wb") as stream:
    writer.write(stream)
```
For longer reports use reportlab.platypus with paragraphs/tables and explicit page breaks.
Render selected pages when Poppler is installed:
```bash
pdftoppm -f 1 -l 1 -scale-to 1600 -png report.pdf preview
```
Visual review requires a connected vision tool. OCR requires an installed OCR engine;
pypdf cannot read the pixels of a scanned page. Parser limits reduce accidental resource
use but do not replace disposable isolation for hostile files.
