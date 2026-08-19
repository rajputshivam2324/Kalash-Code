"""Universal clipboard engine for the Kalash TUI.

Provides multi-backend clipboard copying:
1. Textual native App.copy_to_clipboard()
2. OSC 52 terminal escape sequence (works over SSH, tmux, mosh)
3. Native desktop utilities (wl-copy on Wayland, xclip/xsel on X11, pbcopy on macOS)
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from typing import Any


def copy_text(text: str, app: Any | None = None) -> tuple[bool, str]:
    """Copy text using the best available clipboard mechanism.

    Returns (success, backend_name).
    """
    if not text:
        return False, "empty text"

    success = False
    used_backends: list[str] = []

    # 1. Textual native clipboard (if app instance supplied)
    if app is not None and hasattr(app, "copy_to_clipboard"):
        try:
            app.copy_to_clipboard(text)
            success = True
            used_backends.append("textual")
        except Exception:
            pass

    # 2. OSC 52 escape sequence (works across remote SSH & modern terminals)
    try:
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        # Try both BEL and ST terminated sequences for maximum compatibility
        osc52 = f"\x1b]52;c;{payload}\x07"
        if sys.__stdout__ is not None:
            sys.__stdout__.write(osc52)
            sys.__stdout__.flush()
            success = True
            used_backends.append("osc52")
    except Exception:
        pass

    # 3. Native desktop clipboard utilities
    desktop_backend = _copy_desktop(text)
    if desktop_backend:
        success = True
        used_backends.append(desktop_backend)

    if success:
        return True, "+".join(used_backends)
    return False, "no supported clipboard backend found"


def _copy_desktop(text: str) -> str | None:
    """Attempt desktop clipboard commands."""
    # Wayland
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        try:
            subprocess.run(
                ["wl-copy"],
                input=text.encode("utf-8"),
                check=True,
                timeout=1.5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return "wl-copy"
        except Exception:
            pass

    # X11 xclip
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode("utf-8"),
                check=True,
                timeout=1.5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return "xclip"
        except Exception:
            pass

    # X11 xsel
    if os.environ.get("DISPLAY") and shutil.which("xsel"):
        try:
            subprocess.run(
                ["xsel", "-b", "-i"],
                input=text.encode("utf-8"),
                check=True,
                timeout=1.5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return "xsel"
        except Exception:
            pass

    # macOS pbcopy
    if sys.platform == "darwin" and shutil.which("pbcopy"):
        try:
            subprocess.run(
                ["pbcopy"],
                input=text.encode("utf-8"),
                check=True,
                timeout=1.5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return "pbcopy"
        except Exception:
            pass

    return None


def extract_last_code_block(text: str) -> str | None:
    """Extract the contents of the last markdown code block if present."""
    import re
    blocks = re.findall(r"```(?:\w+)?\n(.*?)```", text, re.DOTALL)
    if blocks:
        return blocks[-1].strip()
    return None
