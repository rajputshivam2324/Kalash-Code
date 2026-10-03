# Safe archive operations
Create archives with explicit member names:
```python
from zipfile import ZipFile

with ZipFile("deliverable.zip", "w") as archive:
    archive.write("report.txt", arcname="report.txt")
with ZipFile("deliverable.zip") as archive:
    assert archive.namelist() == ["report.txt"]
```
For ZIP extraction use a NEW dedicated directory, inspect every entry before writing,
resolve destination paths and require them to remain inside that directory. Reject
absolute paths, parent traversal, symlinks, duplicate destinations, encrypted members,
excessive compression ratios and total expanded size. Stream bounded member bytes;
never extract blindly. For TAR reject symbolic/hard links, devices/FIFOs and use Python's
`data` filter plus independent member/size limits. Do not assume a filter limits resource
use. Existing destination symlinks also require inspection; prefer an empty destination.
The built-in inspector only lists names/sizes and never extracts or follows archive links.
Names such as ../../payload are archive data, not authorized filesystem destinations.
