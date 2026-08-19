---
name: tui-design-patterns
description: Design principles, widget architectures, clipboard OSC 52 integration, streaming animations, and comparison table renderers for Textual TUIs.
version: 1.0.0
allowed-tools: [read, edit, write, shell]
---

# Textual TUI Modern Design & Component Patterns

Use this skill when designing or upgrading terminal user interfaces (TUIs) in Kalash.

## 1. Core Architectural Pillars for Coding Agent TUIs
- **Live Streaming with Zero Flicker**: Stream tokens smoothly into a markdown container while buffering partial ANSI/markdown chunks to prevent layout recalculation thrashing.
- **Collapsible Tool Call Blocks**: Render tool calls with live spinner, elapsed millisecond/second stopwatch, collapsible parameters/output, and color-coded status badges (`✓ complete`, `✗ failed`, `⏳ running`, `🛑 gated`).
- **OSC 52 & Universal Clipboard**: Support universal terminal clipboard copying using OSC 52 escape sequences (`\033]52;c;{base64}\a`) alongside desktop clipboard backends, allowing copy operations over SSH, tmux, and local terminal emulators.
- **Rich Diff & Comparison Tables**: Render unified/side-by-side diff viewers with highlighted gutters (`+`, `-`, `@@`), structural headers, and side-by-side benchmark comparison tables.
- **Interactive Modals & Slash Commands**: Fast fuzzy picker for models (`/model`), evaluation harness visualizer (`/eval`), memory inspector (`/memory`), and session stats (`/status`).

## 2. Clipboard Integration (OSC 52 Protocol)
```python
import base64
import sys

def copy_to_clipboard(text: str) -> bool:
    """Copy text to clipboard using OSC 52 escape sequence."""
    try:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        # OSC 52 sequence: ESC ] 52 ; c ; <base64> BEL
        osc52 = f"\033]52;c;{encoded}\a"
        sys.stdout.write(osc52)
        sys.stdout.flush()
        return True
    except Exception:
        return False
```

## 3. Tool Call Line & Streaming State
- Header: `▸ [tool_name] arguments_summary` (Click or `Enter` to expand/collapse).
- Status badge: Animated dots during execution, duration pill `(1.2s)`, return exit code.
- Body: Monospaced syntax-highlighted block with copy button shortcut (`ctrl+y`).

## 4. Benchmark & Comparison Matrix Layout
Tables rendered with Rich `Table` inside Textual `Static` or `DataTable`:
- Columns: `Component / Benchmark`, `Baseline`, `Candidate / Harness`, `Delta %`, `Status Pill`.
- Color hierarchy: Green (`#10b981`), Amber (`#f59e0b`), Red (`#ef4444`), Muted Cyan (`#38bdf8`), Neutral Dark (`#1e293b`).
