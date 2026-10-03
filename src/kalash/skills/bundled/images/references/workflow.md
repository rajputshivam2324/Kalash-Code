# Raster transformations
```python
from PIL import Image, ImageOps

with Image.open("source.png") as image:
    image = ImageOps.exif_transpose(image)
    image.thumbnail((1200, 1200))
    image.save("resized.png")
with Image.open("resized.png") as check:
    check.verify()
with Image.open("resized.png") as check:
    assert max(check.size) <= 1200
```
Preserve RGBA for transparent PNG/WebP output. JPEG has no alpha: composite against an
explicit chosen background before converting to RGB. Respect animation frame/duration
requirements; a default save may flatten animation. Do not disable decompression-bomb
checks. SVG is vector/code content and should be edited as such, not rasterized by default.
The CLI gives metadata; a configured vision tool must read pixels to describe the image.
No image generation service is bundled. Use tool_search for one if configured.
