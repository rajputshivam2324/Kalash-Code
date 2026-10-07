"""Bounded resident transcript with private, ordered scrollback storage."""

from __future__ import annotations

import tempfile
from typing import TYPE_CHECKING, Any

from textual.containers import VerticalScroll

if TYPE_CHECKING:
    from textual import events
    from textual.widget import Widget


class Transcript(VerticalScroll):
    follow_output = True
    MAX_WIDGETS = 200
    MAX_RESIDENT_BYTES = 2_000_000
    MAX_ARCHIVE_BYTES = 64_000_000

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # on_unmount closes this lifecycle-owned handle.
        self._archive = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")  # noqa: SIM115
        self._archive_bytes = 0
        self.archive_error = ""

    @property
    def archived_text(self) -> str:
        self._archive.flush()
        self._archive.seek(0)
        return self._archive.read()

    def clear_archive(self) -> None:
        self._archive.seek(0)
        self._archive.truncate()
        self._archive_bytes = 0
        self.archive_error = ""

    async def prune(self, protected: set[Widget]) -> None:
        children = list(self.children)
        resident = sum(len(str(getattr(child, "plain_text", "")).encode()) for child in children)
        height = 0
        while len(children) > self.MAX_WIDGETS or resident > self.MAX_RESIDENT_BYTES:
            oldest = children[0]
            if oldest in protected:
                break  # Never evict a widget while its event handlers still own it.
            text = str(getattr(oldest, "plain_text", ""))
            entry = text + "\n\n" if text else ""
            size = len(entry.encode())
            if self._archive_bytes + size <= self.MAX_ARCHIVE_BYTES:
                self._archive.seek(0, 2)
                self._archive.write(entry)
                self._archive_bytes += size
            else:
                self.archive_error = "Scrollback archive exhausted; export is incomplete."
            resident -= len(text.encode())
            height += oldest.outer_size.height
            await oldest.remove()
            children.pop(0)
        if height and not self.follow_output:
            self.scroll_to(y=max(0, self.scroll_y - height), animate=False, immediate=True)

    def on_unmount(self) -> None:
        self._archive.close()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        # Layout growth changes max_scroll_y before the deferred tail scroll.
        # Only reader input disables following; a layout notification cannot.
        if new_value >= self.max_scroll_y - 1:
            self.follow_output = True

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self.follow_output = False
        event.stop()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
