"""Shell entrypoint; mutations and rendering use task-loaded skill recipes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .inspect import inspect_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect selected artifact units without mutation")
    parser.add_argument("path", type=Path)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--sheet")
    parser.add_argument("--max-chars", type=int, default=16_000)
    args = parser.parse_args()
    try:
        result = inspect_artifact(**vars(args))
    except Exception as exc:
        sys.stderr.write(f"Artifact inspection failed: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
