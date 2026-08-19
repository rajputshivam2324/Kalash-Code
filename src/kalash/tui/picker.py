"""Selection picker shown directly above the prompt.

Drives slash commands, provider selection, and model selection. Keyboard
only: up/down moves, enter confirms, escape dismisses. Renders as a single
Static so it participates in normal widget layout without container quirks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rich.text import Text
from textual.widgets import Static


class PickerMode(StrEnum):
    """What the picker is currently selecting."""

    CLOSED = "closed"
    COMMAND = "command"
    PROVIDER = "provider"
    MODEL = "model"
    SESSION = "session"
    THEME = "theme"


@dataclass(frozen=True, slots=True)
class PickerItem:
    """One selectable row."""

    value: str
    label: str
    detail: str = ""


class Picker(Static):
    """Filterable, keyboard-navigable selection list."""

    DEFAULT_CSS = """
    Picker {
        height: 3;
        width: 100%;
        display: none;
        border: round $accent;
        padding: 0 1;
    }
    Picker.visible {
        display: block;
    }
    """

    MAX_VISIBLE = 10

    # Border occupies one row top and bottom.
    _CHROME_ROWS = 2

    def __init__(self) -> None:
        super().__init__(Text(""))
        self.mode: PickerMode = PickerMode.CLOSED
        self._all: list[PickerItem] = []
        self._shown: list[PickerItem] = []
        self._index = 0
        self._offset = 0

    # -- lifecycle ---------------------------------------------------------

    def open(self, mode: PickerMode, items: list[PickerItem], query: str = "") -> None:
        """Open in a given mode with a set of items."""
        self.mode = mode
        self._all = items
        self._index = 0
        self._offset = 0
        self.filter(query)
        self.add_class("visible")

    def close(self) -> None:
        self.mode = PickerMode.CLOSED
        self._all = []
        self._shown = []
        self._index = 0
        self._offset = 0
        self.remove_class("visible")
        self._set_rows(1)
        self.update(Text(""))

    @property
    def is_open(self) -> bool:
        return self.mode is not PickerMode.CLOSED

    @property
    def has_items(self) -> bool:
        return bool(self._shown)

    def show_status(self, message: str) -> None:
        """Show a transient message without entering a select mode.

        Not named `set_loading`: that is real Textual `Widget` API taking a
        bool, and shadowing it both breaks the framework's loading indicator
        and silently mis-renders when Textual calls it internally.
        """
        self.add_class("visible")
        self._set_rows(1)
        self.update(Text(f"  {message}", style="dim"))

    def _set_rows(self, rows: int) -> None:
        """Pin an explicit height.

        Textual 0.89 cannot resolve `height: auto` for a widget that toggles
        visibility inside a `dock: bottom` container whose own height is auto —
        the reflow raises and the app stops processing events. Setting a
        concrete height sidesteps that entirely.
        """
        self.styles.height = max(1, rows) + self._CHROME_ROWS

    # -- filtering and navigation -----------------------------------------

    def filter(self, query: str) -> None:
        """Narrow visible items by substring."""
        needle = query.lstrip("/").strip().lower()
        if needle:
            self._shown = [
                item
                for item in self._all
                if needle in item.value.lower() or needle in item.label.lower()
            ]
        else:
            self._shown = list(self._all)
        self._index = 0
        self._offset = 0
        self._repaint()

    def move(self, delta: int) -> None:
        """Move the selection cursor, scrolling the window as needed."""
        if not self._shown:
            return
        self._index = max(0, min(len(self._shown) - 1, self._index + delta))
        if self._index < self._offset:
            self._offset = self._index
        elif self._index >= self._offset + self.MAX_VISIBLE:
            self._offset = self._index - self.MAX_VISIBLE + 1
        self._repaint()

    def selected(self) -> PickerItem | None:
        """The highlighted item, if any."""
        if self._shown and 0 <= self._index < len(self._shown):
            return self._shown[self._index]
        return None

    # -- rendering ---------------------------------------------------------

    def _repaint(self) -> None:
        """Rebuild the rendered list.

        Deliberately not named `_render`: `Widget._render` is internal Textual
        API that must return a visual, and shadowing it makes every paint fail
        with an opaque NoneType error.
        """
        if not self._shown:
            self._set_rows(1)
            self.update(Text("  no matches", style="dim"))
            return

        window = self._shown[self._offset : self._offset + self.MAX_VISIBLE]
        label_width = max((len(item.label) for item in window), default=0)
        overflow = 1 if len(self._shown) > len(window) else 0
        self._set_rows(len(window) + overflow)
        text = Text()

        for position, item in enumerate(window):
            if position:
                text.append("\n")
            selected = (self._offset + position) == self._index
            text.append("› " if selected else "  ", style="bold cyan" if selected else "")
            text.append(
                item.label.ljust(label_width),
                style="bold reverse" if selected else "bold",
            )
            if item.detail:
                text.append("  ")
                text.append(item.detail, style="dim")

        remaining = len(self._shown) - len(window)
        if remaining > 0:
            text.append(f"\n  … {remaining} more", style="dim")

        self.update(text)
