"""Run outside the source tree in an isolated base-wheel installation; no API calls."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    import kalash
    from kalash.core.config import KalashConfig
    from kalash.mcp.schema import validate_arguments, validator_for
    from kalash.sdk import KalashClient

    location = Path(kalash.__file__).resolve()
    assert "site-packages" in location.parts, f"Source tree shadowed wheel: {location}"
    output_limit = KalashConfig().model.max_output_tokens
    assert output_limit is None or output_limit > 0
    assert KalashClient is not None
    assert validate_arguments(validator_for({"type": "object"}), {}) == {}
    resource_root = importlib.resources.files("kalash.skills") / "bundled"
    hashes = {}
    for directory in resource_root.iterdir():
        if not directory.is_dir():
            continue
        skill = directory / "SKILL.md"
        if skill.is_file():
            hashes[str(directory.name) + "/SKILL.md"] = hashlib.sha256(
                skill.read_bytes()
            ).hexdigest()
        references = directory / "references"
        if references.is_dir():
            for resource in references.iterdir():
                if resource.is_file():
                    hashes[f"{directory.name}/references/{resource.name}"] = hashlib.sha256(
                        resource.read_bytes()
                    ).hexdigest()
    expected_root = Path(__file__).resolve().parents[1] / "src/kalash/skills/bundled"
    expected = {
        str(path.relative_to(expected_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in expected_root.rglob("*.md")
    }
    assert expected
    assert hashes == expected, "Wheel resources differ from source bytes"
    with tempfile.TemporaryDirectory(prefix="kalash-wheel-home-") as home:
        environment = {**os.environ, "KALASH_HOME": home}
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "-m", "kalash", "--help"],
            cwd=home,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "doctor" in result.stdout
    sys.stdout.write(
        json.dumps(
            {
                "passed": True,
                "python": sys.version,
                "package": str(location),
                "version": importlib.metadata.version("kalash-code"),
                "resources": hashes,
                "dependencies": {
                    dist.metadata["Name"]: dist.version
                    for dist in importlib.metadata.distributions()
                },
                "scope": "base wheel imports, CLI help, local MCP schema "
                "and bundled resource bytes",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
