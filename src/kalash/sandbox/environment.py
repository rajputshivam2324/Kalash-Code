"""Operating environment passed to tool and extension subprocesses."""

import os


def safe_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "TZ", "SYSTEMROOT"}
    return {
        key: value for key, value in os.environ.items() if key in allowed or key.startswith("LC_")
    }
