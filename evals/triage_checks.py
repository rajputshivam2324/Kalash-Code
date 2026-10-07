"""Independent CLI acceptance checks, copied only into the validation workspace."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SEVERITIES = ("critical", "high", "medium", "low")


def invoke(store: Path, *args: str, success: bool = True) -> Any:
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "triage.py"), "--store", str(store), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if not success:
        assert result.returncode != 0, "Invalid operation succeeded"
        assert result.stderr.strip(), "Missing actionable stderr"
        assert "Traceback" not in result.stderr, "Leaked traceback"
        return result
    assert result.returncode == 0, result.stderr[:2000]
    return json.loads(result.stdout)


def help_check(_store: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "triage.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert result.returncode == 0
    assert all(command in result.stdout for command in ("add", "list", "close", "summary"))


def empty_reads(store: Path) -> None:
    assert invoke(store, "list") == []
    assert invoke(store, "summary") == {
        "open": 0,
        "closed": 0,
        "by_severity": dict.fromkeys(SEVERITIES, 0),
    }
    assert not store.exists(), "Read unexpectedly created a store"


def create_restart_unicode(store: Path) -> None:
    first = invoke(store, "add", "  भुगतान 🚨  ", "--severity", "high")
    assert type(first["id"]) is int
    assert first["id"] > 0
    assert first["title"] == "भुगतान 🚨"
    assert first["severity"] == "high"
    assert first["status"] == "open"
    second = invoke(store, "add", "Queue lag")
    assert second["id"] > first["id"]
    assert second["severity"] == "medium"
    assert invoke(store, "list") == [first, second]
    json.loads(store.read_text(encoding="utf-8"))


def sort_filter(store: Path) -> None:
    incidents = [
        invoke(store, "add", str(i), "--severity", severity)
        for i, severity in enumerate(("low", "high", "critical", "high", "medium"))
    ]
    ordered = sorted(incidents, key=lambda row: (SEVERITIES.index(row["severity"]), row["id"]))
    assert invoke(store, "list") == ordered
    assert invoke(store, "list", "--severity", "high") == [incidents[1], incidents[3]]
    invoke(store, "close", str(incidents[1]["id"]))
    assert invoke(store, "list", "--status", "closed", "--severity", "high") == [
        {**incidents[1], "status": "closed"}
    ]
    assert len(invoke(store, "list", "--status", "open")) == 4


def close_idempotent(store: Path) -> None:
    incident = invoke(store, "add", "Disk full")
    closed = invoke(store, "close", str(incident["id"]))
    assert closed == {**incident, "status": "closed"}
    assert invoke(store, "close", str(incident["id"])) == closed
    later = invoke(store, "add", "Next incident")
    assert later["id"] > incident["id"]


def unknown_id(store: Path) -> None:
    invoke(store, "close", "99999", success=False)
    assert not store.exists()
    invoke(store, "add", "Known incident")
    before = store.read_bytes()
    invoke(store, "close", "99999", success=False)
    assert store.read_bytes() == before


def invalid_input(store: Path) -> None:
    invoke(store, "add", "Valid incident")
    before = store.read_bytes()
    for args in (
        ("add", " \t "),
        ("add", "Invalid", "--severity", "urgent"),
        ("close", "not-an-id"),
        ("list", "--status", "unknown"),
    ):
        invoke(store, *args, success=False)
        assert store.read_bytes() == before


def corrupt_schema(store: Path) -> None:
    # An empty list may be a legitimate store format; the task does not prescribe one.
    for content in ("{broken", "null", "[null]", '{"next_id":false}'):
        store.write_text(content)
        for args in (("list",), ("add", "Must not overwrite")):
            invoke(store, *args, success=False)
            assert store.read_text() == content


def nested_store(store: Path) -> None:
    nested = store.parent / "nested" / "deeper" / "incidents.json"
    incident = invoke(nested, "add", "Nested path")
    assert nested.is_file()
    assert invoke(nested, "list") == [incident]


def summary(store: Path) -> None:
    rows = [invoke(store, "add", severity, "--severity", severity) for severity in SEVERITIES]
    invoke(store, "close", str(rows[0]["id"]))
    assert invoke(store, "summary") == {
        "open": 3,
        "closed": 1,
        "by_severity": {"critical": 0, "high": 1, "medium": 1, "low": 1},
    }


CHECKS = {
    "help": help_check,
    "empty_reads": empty_reads,
    "create_restart_unicode": create_restart_unicode,
    "sort_filter": sort_filter,
    "close_idempotent": close_idempotent,
    "unknown_id": unknown_id,
    "invalid_input": invalid_input,
    "corrupt_schema": corrupt_schema,
    "nested_store": nested_store,
    "summary": summary,
}


def main() -> None:
    results = []
    for name, check in CHECKS.items():
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            try:
                assert (ROOT / "triage.py").is_file(), "Required triage.py is missing"
                check(Path(directory) / "incidents.json")
            except Exception as exc:
                results.append(
                    {"id": name, "passed": False, "detail": f"{type(exc).__name__}: {exc}"[:2500]}
                )
            else:
                results.append({"id": name, "passed": True, "detail": "Verified"})
    sys.stdout.write(json.dumps(results, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
