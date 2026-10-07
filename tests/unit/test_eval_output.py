"""Evaluation evidence survives loss of temporary directories."""

from pathlib import Path

import pytest
from evals.output import default_output


def test_default_evidence_location_is_persistent_unique_and_cwd_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    first = default_output("compact")
    second = default_output("compact")
    assert first.parent == Path(__file__).resolve().parents[2] / ".kalash" / "eval_runs"
    assert first != second
    assert not first.exists()


@pytest.mark.parametrize("kind", ["", "../escape", "a/b", "bad\\name"])
def test_default_evidence_kind_cannot_escape_run_root(kind: str) -> None:
    with pytest.raises(ValueError, match="Evaluation kind"):
        default_output(kind)
