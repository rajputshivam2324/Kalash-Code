"""Guard against widget methods shadowing Textual internals.

Two bugs shipped from this exact mistake:

* `Picker._render` overrode `Widget._render`, which Textual calls internally and
  expects to return a visual. Returning `None` made every paint fail with an
  opaque `AttributeError`, and because Textual swallows reflow errors the app
  kept running with a dead layout — so it looked like the keyboard was broken.
* `Picker.set_loading` overrode `Widget.set_loading(bool)` with a `str`
  signature, breaking the framework's loading indicator.

Both were invisible to feature tests. This checks the property directly, so any
new widget method that collides fails here instead of at runtime.
"""

from __future__ import annotations

import inspect

import pytest
from textual.widget import Widget
from textual.widgets import Static

import kalash.tui.messages as messages_module
import kalash.tui.picker as picker_module


# Overriding these is the documented way to build a widget.
INTENTIONAL_OVERRIDES = frozenset(
    {
        "compose",
        "render",
        "on_mount",
        "on_unmount",
        "watch_",  # prefix, handled below
    }
)

FRAMEWORK_NAMES = frozenset(dir(Widget)) | frozenset(dir(Static))


def _widget_classes():
    """Every Widget subclass Kalash defines in the TUI package."""
    for module in (messages_module, picker_module):
        for name, obj in vars(module).items():
            if not inspect.isclass(obj):
                continue
            if not issubclass(obj, Widget):
                continue
            # Only classes defined in our module, not imported bases.
            if obj.__module__ != module.__name__:
                continue
            yield name, obj


def _own_callables(cls: type) -> set[str]:
    return {
        name
        for name, value in vars(cls).items()
        if callable(value) and not name.startswith("__")
    }


def _is_allowed(name: str) -> bool:
    if name in INTENTIONAL_OVERRIDES:
        return True
    return name.startswith(("watch_", "action_", "on_", "key_"))


@pytest.mark.parametrize("class_name,cls", list(_widget_classes()))
def test_no_framework_method_shadowing(class_name: str, cls: type) -> None:
    collisions = sorted(
        name
        for name in _own_callables(cls)
        if name in FRAMEWORK_NAMES and not _is_allowed(name)
    )
    assert not collisions, (
        f"{class_name} defines {collisions}, which already exist on Textual's "
        f"Widget/Static. Rename them — shadowing framework methods causes "
        f"failures far from the cause."
    )


def test_guard_actually_detects_a_collision() -> None:
    """The guard must fail on a real collision, not vacuously pass."""

    class Offender(Static):
        def _render(self) -> None:  # shadows Widget._render
            return None

    collisions = {
        name
        for name in _own_callables(Offender)
        if name in FRAMEWORK_NAMES and not _is_allowed(name)
    }
    assert "_render" in collisions


def test_widget_classes_were_discovered() -> None:
    """A discovery bug would make the parametrized test vacuous."""
    found = {name for name, _ in _widget_classes()}
    assert "Picker" in found
    assert "AssistantMessage" in found
    assert len(found) >= 5
