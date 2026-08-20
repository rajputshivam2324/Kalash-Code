"""Tests for the LLM-as-a-Judge evaluation framework."""

import pytest
from pathlib import Path
from evals.judge import evaluate_content_deterministic, run_binary_search_notes_judge


def test_deterministic_judge_scoring(tmp_path: Path):
    sample_doc = """# Binary Search Fundamentals

## Invariants
Binary search operates on monotonic search spaces.
Search range invariants are preserved throughout iterations.

## Midpoint Calculation
To avoid integer overflow:
```python
def binary_search(nums: list[int], target: int) -> int:
    \"\"\"Perform binary search with overflow-safe midpoint.\"\"\"
    low, high = 0, len(nums) - 1
    while low <= high:
        mid = low + (high - low) // 2
        if nums[mid] == target:
            return mid
        elif nums[mid] < target:
            low = mid + 1
        else:
            high = mid - 1
    return -1
```

| Interval | Loop Condition | Left Update | Right Update |
| :--- | :--- | :--- | :--- |
| `[low, high]` | `while low <= high:` | `low = mid + 1` | `high = mid - 1` |
"""
    scorecard = evaluate_content_deterministic(
        "test_fundamentals.md",
        sample_doc,
        required_keywords=["monotonic", "invariant", "overflow", "interval"],
        required_code_patterns=[r"while\s+low\s*<=\s*high", r"mid\s*=\s*low\s*\+\s*\(high\s*-\s*low\)"],
    )

    assert scorecard.passed is True
    assert scorecard.overall_score >= 8.5
    assert len(scorecard.dimensions) == 4
    assert any(d.name == "correctness" and d.score >= 9.0 for d in scorecard.dimensions)


@pytest.mark.asyncio
async def test_run_binary_search_notes_judge(tmp_path: Path):
    # Setup dummy chapters
    (tmp_path / "01_fundamentals.md").write_text("""# Fundamentals
Monotonic search space and template invariants.
```python
def search(nums: list[int], target: int) -> int:
    low, high = 0, len(nums) - 1
    while low <= high:
        mid = low + (high - low) // 2
        if nums[mid] == target: return mid
        elif nums[mid] < target: low = mid + 1
        else: high = mid - 1
    return -1
```
| Col1 | Col2 |
|---|---|
| A | B |
""")
    report = await run_binary_search_notes_judge(tmp_path)
    assert report.total == 6
    # Chapter 1 passed, others report missing
    ch1 = next(r for r in report.results if r.name == "chapter_1_fundamentals")
    assert ch1.passed is True
