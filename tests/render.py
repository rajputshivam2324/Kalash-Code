"""Render Rich text, groups and panels consistently in UI assertions."""

from io import StringIO

from rich.console import Console


def plain(renderable) -> str:
    output = StringIO()
    Console(file=output, width=120, color_system=None).print(renderable)
    return output.getvalue()
