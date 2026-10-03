# PPTX recipes
```python
from pptx import Presentation

prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "Project results"
slide.placeholders[1].text = "Verified checks\nRemaining work"
prs.save("results.pptx")
check = Presentation("results.pptx")
assert len(check.slides) == 1
assert check.slides[0].shapes.title.text == "Project results"
```
Use explicit dimensions, consistent typography, contrast and generous spacing. Change
existing text runs while preserving styles where possible. Inspect chart data and notes
with python-pptx when needed; the text inspector does not cover all slide semantics.
Render with installed LibreOffice, then Poppler:
```bash
libreoffice -env:UserInstallation=file:///tmp/kalash-slide-render --headless --convert-to pdf --outdir previews results.pptx
pdftoppm -scale-to 1600 -png previews/results.pdf previews/slide
```
Review pixels with an available vision tool. A successful save or text extraction is not
visual verification. Legacy PPT requires an explicit conversion workflow.
