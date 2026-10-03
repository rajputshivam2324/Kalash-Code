"""Bounded, selective artifact inspection. No extraction, mutation or execution.

Invoke via ``python -m kalash.artifacts`` in the existing shell sandbox. Parsers
are not host tools: their CPU/time and filesystem authority belong to that shell.
"""

from __future__ import annotations

import bz2
import csv
import gzip
import hashlib
import importlib
import io
import json
import lzma
import tarfile
import zipfile
from contextlib import ExitStack
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO
from xml.etree import ElementTree as ET

if TYPE_CHECKING:
    from collections.abc import Iterator

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 10_000


def _optional(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ValueError(
            f"Missing optional parser {module!r}. Install kalash-code[artifacts] "
            "in the execution environment before using this format."
        ) from exc


def _check_zip(archive: zipfile.ZipFile) -> None:
    entries = archive.infolist()
    if len(entries) > MAX_MEMBERS or sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
        raise ValueError("Archive exceeds member/expanded-size limits")
    for entry in entries:
        if (
            entry.file_size > MAX_MEMBER_BYTES
            or entry.file_size > max(entry.compress_size, 1) * 200
        ):
            raise ValueError("Archive member exceeds size/compression-ratio limits")


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    raw = archive.read(name)
    if b"\x00" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("XML declarations/entities are not supported")
    return ET.fromstring(raw)  # noqa: S314 -- bounded XML, DTD/entities rejected above


def _office_units(
    path: Path, kind: str, offset: int, limit: int
) -> tuple[int, list[tuple[str, str]]]:
    with zipfile.ZipFile(path) as archive:
        _check_zip(archive)
        if kind == "docx":
            root = _xml(archive, "word/document.xml")
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            paragraphs = list(root.iter(ns + "p"))
            return len(paragraphs), [
                (f"paragraph {index + 1}", "".join(t.text or "" for t in p.iter(ns + "t")))
                for index, p in enumerate(paragraphs[offset : offset + limit], offset)
            ]
        # Numeric slide order, not lexicographic slide1/slide10/slide2.
        slides = sorted(
            (
                n
                for n in archive.namelist()
                if n.startswith("ppt/slides/slide")
                and n.endswith(".xml")
                and n[len("ppt/slides/slide") : -4].isdigit()
            ),
            key=lambda n: int(n[len("ppt/slides/slide") : -4]),
        )
        ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
        return len(slides), [
            (f"slide {index + 1}", "\n".join(t.text or "" for t in _xml(archive, name).iter(ns)))
            for index, name in enumerate(slides[offset : offset + limit], offset)
        ]


class _LimitedReader(io.RawIOBase):
    """Bound decompressed TAR scanning, including skipped member bodies."""

    def __init__(self, stream: BinaryIO) -> None:
        super().__init__()
        self.stream = stream
        self.remaining = MAX_EXPANDED_BYTES

    def read(self, size: int = -1) -> bytes:
        requested = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        data = self.stream.read(requested)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ValueError("TAR decompressed stream exceeds size limit")
        return data


def _archive_units(path: Path, offset: int, limit: int) -> tuple[int, list[tuple[str, str]]]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_MEMBERS:
                raise ValueError("Archive exceeds member limit")
            return len(entries), [
                (entry.filename, f"size={entry.file_size}; compressed={entry.compress_size}")
                for entry in entries[offset : offset + limit]
            ]
    with ExitStack() as stack:
        opener: Any = {".gz": gzip.open, ".tgz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}.get(
            path.suffix.lower(), Path.open
        )
        stream = stack.enter_context(opener(path, "rb"))
        tar = stack.enter_context(tarfile.open(fileobj=_LimitedReader(stream), mode="r|"))
        selected = []
        count = 0
        for index, member in enumerate(tar):
            count += 1
            if count > MAX_MEMBERS:
                raise ValueError("Archive exceeds member limit")
            if offset <= index < offset + limit:
                selected.append((member.name, f"size={member.size}; type={member.type!r}"))
        return count, selected


def _data_units(path: Path, offset: int, limit: int) -> tuple[int, list[tuple[str, str]]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        items: Iterator[tuple[Any, Any]] = (
            iter(data.items())
            if isinstance(data, dict)
            else enumerate(data)
            if isinstance(data, list)
            else iter([("value", data)])
        )
        count = len(data) if isinstance(data, (dict, list)) else 1
        return count, [
            (str(k), json.dumps(v, ensure_ascii=False))
            for k, v in islice(items, offset, offset + limit)
        ]
    selected = []
    count = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows: Iterator[Any]
        if suffix in {".jsonl", ".ndjson"}:
            rows = (json.loads(line) for line in stream if line.strip())
        else:
            csv.field_size_limit(MAX_MEMBER_BYTES)
            rows = iter(csv.reader(stream, delimiter="\t" if suffix == ".tsv" else ","))
        for index, row in enumerate(rows):
            count += 1
            if offset <= index < offset + limit:
                selected.append((f"row {index + 1}", json.dumps(row, ensure_ascii=False)))
    return count, selected


def _units(
    path: Path, offset: int, limit: int, sheet: str | None
) -> tuple[str, int, list[tuple[str, str]], dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in {".docx", ".pptx"}:
        count, units = _office_units(path, suffix[1:], offset, limit)
        return suffix[1:], count, units, {"view": "text only; layout is not rendered"}
    if suffix in {".json", ".jsonl", ".ndjson", ".csv", ".tsv"}:
        count, units = _data_units(path, offset, limit)
        return suffix[1:], count, units, {}
    if suffix == ".pdf":
        reader = _optional("pypdf").PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF requires an explicit decryption workflow")
        count = len(reader.pages)
        units = [
            (f"page {i + 1}", reader.pages[i].extract_text() or "[no text; OCR may be needed]")
            for i in range(offset, min(offset + limit, count))
        ]
        return "pdf", count, units, {"view": "text only; scanned pages require OCR"}
    if suffix == ".xlsx":
        with zipfile.ZipFile(path) as archive:
            _check_zip(archive)
        workbook = _optional("openpyxl").load_workbook(path, read_only=True, data_only=False)
        try:
            if sheet is None:
                names = workbook.sheetnames
                return (
                    "xlsx-sheets",
                    len(names),
                    [(name, "select with --sheet") for name in names[offset : offset + limit]],
                    {},
                )
            if sheet not in workbook.sheetnames:
                raise ValueError(f"Unknown sheet {sheet!r}; inspect sheet names first")
            ws = workbook[sheet]
            # Bounds apply even to forged workbook dimensions. Formula source
            # is preserved; this parser is not a calculation engine.
            units = [
                (f"row {i + 1}", json.dumps(list(row), default=str, ensure_ascii=False))
                for i, row in enumerate(
                    ws.iter_rows(
                        min_row=offset + 1,
                        max_row=offset + limit,
                        max_col=min(ws.max_column or 100, 100),
                        values_only=True,
                    ),
                    offset,
                )
            ]
            return (
                "xlsx",
                ws.max_row or offset + len(units),
                units,
                {"sheet": sheet, "max_columns": 100, "formulas": "source; not recalculated"},
            )
        finally:
            workbook.close()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"}:
        image = _optional("PIL.Image")
        with image.open(path) as picture:
            if picture.width * picture.height > 40_000_000:
                raise ValueError("Image exceeds pixel limit")
            info = {
                "width": picture.width,
                "height": picture.height,
                "mode": picture.mode,
                "format": picture.format,
                "frames": getattr(picture, "n_frames", 1),
            }
        return (
            "image",
            1,
            [("metadata", json.dumps(info))] if offset == 0 else [],
            {"view": "metadata only; pixels have not been sent to the model"},
        )
    if suffix in {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz"}:
        count, units = _archive_units(path, offset, limit)
        return "archive", count, units, {"view": "member listing; nothing extracted"}
    if suffix in {".md", ".txt", ".py", ".js", ".ts", ".yaml", ".yml", ".toml"}:
        units = []
        count = 0
        with path.open(encoding="utf-8") as stream:
            for i, line in enumerate(stream):
                count += 1
                if offset <= i < offset + limit:
                    units.append((f"line {i + 1}", line.rstrip("\n")))
        # Use the native read/search tools for general code/text navigation.
        return "text", count, units, {}
    raise ValueError(f"Unsupported format {suffix!r}; load the relevant skill for conversion")


def inspect_artifact(
    path: Path,
    *,
    offset: int = 0,
    limit: int = 20,
    sheet: str | None = None,
    max_chars: int = 16_000,
) -> dict[str, Any]:
    """Return provenance and selected pages/slides/rows/members, with output caps."""
    if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100 or not 256 <= max_chars <= 32_000:
        raise ValueError("Inspection range/output exceeds limits")
    path = path.resolve(strict=True)
    before = path.stat()
    size = before.st_size
    if not path.is_file() or size > MAX_FILE_BYTES:
        raise ValueError("Expected a regular file no larger than 32 MiB")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    del raw
    kind, count, selected, metadata = _units(path, offset, limit, sheet)
    after = path.stat()
    if (before.st_ino, size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("Artifact changed during inspection; retry against current file state")
    units = []
    remaining = max_chars
    clipped = False
    for label, text in selected:
        if remaining <= 0:
            clipped = True
            break
        text_limit = min(remaining, 4000)
        clipped |= len(text) > text_limit
        units.append(
            {"label": label[:256], "text": text[:text_limit], "truncated": len(text) > text_limit}
        )
        remaining -= len(text[:text_limit])
    return {
        "path": str(path),
        "sha256": digest,
        "bytes": size,
        "format": kind,
        "offset": offset,
        "count": count,
        "units": units,
        "content_truncated": clipped,
        "next_offset": offset + len(units) if offset + len(units) < count else None,
        **metadata,
    }
