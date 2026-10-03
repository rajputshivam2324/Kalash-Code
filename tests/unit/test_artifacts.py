"""Real file fixtures for artifact inspection and resource/output limits."""

import hashlib
import io
import json
import tarfile
import zipfile
from unittest.mock import patch

import pytest

from kalash.artifacts.__main__ import main
from kalash.artifacts.inspect import inspect_artifact


def test_json_selected_units_have_provenance_and_continuation(tmp_path):
    path = tmp_path / "records.json"
    path.write_text(json.dumps([{"id": i} for i in range(20)]))
    result = inspect_artifact(path, offset=4, limit=2)
    assert result["count"] == 20
    assert [json.loads(u["text"]) for u in result["units"]] == [{"id": 4}, {"id": 5}]
    assert result["next_offset"] == 6
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("extension", "body"),
    [
        ("csv", 'id,description\n001,"comma, value"\n002,last\n'),
        ("tsv", "id\tdescription\n001\tvalue\n002\tlast\n"),
        ("jsonl", '{"id":"header"}\n{"id":"001"}\n{"id":"002"}\n'),
    ],
)
def test_streamed_rows_preserve_values(tmp_path, extension, body):
    path = tmp_path / f"records.{extension}"
    path.write_text(body)
    result = inspect_artifact(path, offset=1, limit=1)
    assert result["count"] == 3
    assert "001" in result["units"][0]["text"]
    assert result["next_offset"] == 2


def test_docx_paragraphs_and_table_text(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "report.docx"
    document = docx.Document()
    document.add_paragraph("first")
    document.add_paragraph("second")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "table evidence"
    document.save(path)
    result = inspect_artifact(path, offset=1, limit=2)
    assert [u["text"] for u in result["units"]] == ["second", "table evidence"]
    assert result["count"] == 3
    assert "layout" in result["view"]


def test_pptx_numeric_slide_order(tmp_path):
    pptx = pytest.importorskip("pptx")
    path = tmp_path / "slides.pptx"
    deck = pptx.Presentation()
    for i in range(12):
        slide = deck.slides.add_slide(deck.slide_layouts[0])
        slide.shapes.title.text = f"Title {i + 1}"
    deck.save(path)
    result = inspect_artifact(path, offset=8, limit=3)
    assert result["count"] == 12
    assert [u["text"].strip() for u in result["units"]] == ["Title 9", "Title 10", "Title 11"]


def test_xlsx_sheet_catalog_and_formula_source(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "book.xlsx"
    book = openpyxl.Workbook()
    book.active.title = "Summary"
    book.active.append(["001", 4])
    book.active.append(["002", "=B1*2"])
    book.save(path)
    catalog = inspect_artifact(path)
    assert catalog["units"][0]["label"] == "Summary"
    result = inspect_artifact(path, sheet="Summary", offset=1, limit=1)
    assert json.loads(result["units"][0]["text"])[:2] == ["002", "=B1*2"]
    assert "not recalculated" in result["formulas"]
    with pytest.raises(ValueError, match="Unknown sheet"):
        inspect_artifact(path, sheet="missing")


def test_pdf_page_selection_and_creation_recipe(tmp_path):
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    pytest.importorskip("pypdf")
    path = tmp_path / "report.pdf"
    document = canvas.Canvas(str(path))
    for i in range(3):
        document.drawString(72, 750, f"Evidence page {i + 1}")
        document.showPage()
    document.save()
    result = inspect_artifact(path, offset=1, limit=1)
    assert result["count"] == 3
    assert "Evidence page 2" in result["units"][0]["text"]
    assert "Evidence page 1" not in str(result["units"])


def test_image_metadata_and_alpha_transformation(tmp_path):
    image = pytest.importorskip("PIL.Image")
    source = tmp_path / "source.png"
    output = tmp_path / "resized.png"
    image.new("RGBA", (80, 40), (255, 0, 0, 0)).save(source)
    with image.open(source) as picture:
        picture.thumbnail((20, 20))
        picture.save(output)
    result = inspect_artifact(output)
    metadata = json.loads(result["units"][0]["text"])
    assert (metadata["width"], metadata["height"], metadata["mode"]) == (20, 10, "RGBA")
    assert "not been sent" in result["view"]


@pytest.mark.parametrize("extension", ["zip", "tar", "tgz"])
def test_archive_listing_never_extracts_traversal(tmp_path, extension):
    path = tmp_path / f"archive.{extension}"
    if extension == "zip":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("../../payload", "data")
    else:
        with tarfile.open(path, "w:gz" if extension == "tgz" else "w") as archive:
            member = tarfile.TarInfo("../../payload")
            member.size = 4
            archive.addfile(member, io.BytesIO(b"data"))
    result = inspect_artifact(path)
    assert result["units"][0]["label"] == "../../payload"
    assert list(tmp_path.iterdir()) == [path]


def test_xml_entities_and_compressed_office_bomb_are_rejected(tmp_path):
    path = tmp_path / "malicious.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY x "bad">]><x>&x;</x>')
    with pytest.raises(ValueError, match="declarations/entities"):
        inspect_artifact(path)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "A" * 100_000)
    with pytest.raises(ValueError, match="compression-ratio"):
        inspect_artifact(path)


def test_decompressed_tar_budget_and_output_limits(tmp_path, monkeypatch):
    path = tmp_path / "expanded.tgz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("large.txt")
        member.size = 40_000
        archive.addfile(member, io.BytesIO(b"a" * member.size))
    monkeypatch.setattr("kalash.artifacts.inspect.MAX_EXPANDED_BYTES", 1000)
    with pytest.raises(ValueError, match="decompressed stream"):
        inspect_artifact(path)
    path = tmp_path / "huge.json"
    path.write_text(json.dumps(["a" * 10_000] * 10))
    result = inspect_artifact(path, max_chars=256)
    assert sum(len(u["text"]) for u in result["units"]) <= 256
    assert result["content_truncated"]
    assert result["next_offset"] == 1


def test_missing_dependency_and_cli_errors_are_actionable(tmp_path, capsys):
    path = tmp_path / "file.pdf"
    path.write_bytes(b"%PDF")
    with (
        patch("kalash.artifacts.inspect.importlib.import_module", side_effect=ImportError),
        pytest.raises(ValueError, match=r"kalash-code\[artifacts\]"),
    ):
        inspect_artifact(path)
    with patch("sys.argv", ["kalash.artifacts", str(tmp_path / "missing")]):
        assert main() == 1
    assert "inspection failed" in capsys.readouterr().err


def test_text_count_and_range_validation(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("one\ntwo\nthree\n")
    assert inspect_artifact(path, limit=1)["count"] == 3
    assert inspect_artifact(path, offset=10)["units"] == []
    with pytest.raises(ValueError, match="limits"):
        inspect_artifact(path, limit=101)
