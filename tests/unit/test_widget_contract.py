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
        name for name, value in vars(cls).items() if callable(value) and not name.startswith("__")
    }


def _is_allowed(name: str) -> bool:
    if name in INTENTIONAL_OVERRIDES:
        return True
    return name.startswith(("watch_", "action_", "on_", "key_"))


@pytest.mark.parametrize("class_name,cls", list(_widget_classes()))
def test_no_framework_method_shadowing(class_name: str, cls: type) -> None:
    collisions = sorted(
        name for name in _own_callables(cls) if name in FRAMEWORK_NAMES and not _is_allowed(name)
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


# ---------------------------------------------------------------------------
# App-level shadowing
# ---------------------------------------------------------------------------
#
# The widget guard above only inspects Widget subclasses, so it missed a third
# instance of the same mistake at the App level:
#
#   KalashApp._current_mode() collided with `self._current_mode: str`, which
#   Textual's App.__init__ assigns for its screen-modes feature. An *instance*
#   attribute shadows a method of the same name, so `self._current_mode()`
#   resolved to the string "_default" and raised
#   `TypeError: 'str' object is not callable` — only on the resume path, which
#   no unit test covered.
#
# Class-level `dir(App)` does not reveal it, because the attribute is created in
# __init__. These tests instantiate to catch that.


def _textual_app_instance_attrs() -> frozenset[str]:
    """Attribute names Textual's App assigns to every instance."""
    from textual.app import App

    class Bare(App):
        pass

    return frozenset(Bare().__dict__)


def test_kalash_app_defines_no_shadowed_methods() -> None:
    from kalash.tui.app import KalashApp

    declared = {
        name
        for name, value in vars(KalashApp).items()
        if callable(value) and not name.startswith("__")
    }
    collisions = sorted(declared & _textual_app_instance_attrs())
    assert not collisions, (
        f"KalashApp defines {collisions}, which Textual's App.__init__ also "
        f"assigns as instance attributes. The instance attribute wins, so these "
        f"methods are unreachable at runtime. Rename them."
    )


def test_every_kalash_app_method_is_actually_callable() -> None:
    """Direct check on a live instance, independent of how the clash arises."""
    from kalash.tui.app import KalashApp

    app = KalashApp()
    declared = [
        name
        for name, value in vars(KalashApp).items()
        if callable(value) and not name.startswith("__")
    ]
    broken = [
        (name, type(getattr(app, name, None)).__name__)
        for name in declared
        if not callable(getattr(app, name, None))
    ]
    assert not broken, f"shadowed by a non-callable instance attribute: {broken}"


def test_app_guard_detects_a_real_collision() -> None:
    """The guard must fail on a genuine clash, not vacuously pass."""
    from textual.app import App

    class Offender(App):
        def _current_mode(self) -> str:  # shadowed by App.__init__
            return "unreachable"

    instance = Offender()
    assert not callable(instance._current_mode)
    assert "_current_mode" in _textual_app_instance_attrs()


# ---------------------------------------------------------------------------
# Comprehensive shadowing guard
# ---------------------------------------------------------------------------
#
# This bug class has now shipped four times, each time in a different Textual base
# class, and each time the previous guard did not cover the new location:
#
#   1. Picker._render          shadowed Widget._render      -> all painting failed
#   2. Picker.set_loading      shadowed Widget.set_loading  -> loading indicator
#   3. KalashApp._current_mode shadowed an App instance attr -> resume crashed
#   4. ApprovalScreen._context shadowed MessagePump._context -> approval modal
#      mounted but its message pump raised on every message, so the dialog could
#      never be answered and the agent waited forever. Visible as a freeze.
#
# The guards above only scanned two modules and only Widget/Static. This one walks
# every class in every kalash.tui module and checks against the full framework
# surface, including attributes created in __init__.


def _framework_surface() -> frozenset[str]:
    """Every name Textual's base classes occupy, class-level and instance-level."""
    from textual.app import App
    from textual.message_pump import MessagePump
    from textual.screen import ModalScreen, Screen
    from textual.widget import Widget
    from textual.widgets import Input, Static

    names: set[str] = set()
    for cls in (MessagePump, Widget, Static, Screen, ModalScreen, App, Input):
        names.update(dir(cls))

    # Attributes assigned during __init__ never appear in dir(cls).
    class BareApp(App):
        pass

    names.update(BareApp().__dict__)
    try:
        names.update(Static("x").__dict__)
    except Exception:  # pragma: no cover - defensive
        pass
    return frozenset(names)


def _kalash_tui_classes():
    """Every Textual widget/screen/app class defined in the kalash.tui package."""
    import importlib
    import pkgutil

    from textual.message_pump import MessagePump

    import kalash.tui as tui_package

    for module_info in pkgutil.iter_modules(tui_package.__path__):
        module = importlib.import_module(f"kalash.tui.{module_info.name}")
        for name, obj in vars(module).items():
            if (
                inspect.isclass(obj)
                and obj.__module__ == module.__name__
                and not name.startswith("_")
                and issubclass(obj, MessagePump)
                and obj is not MessagePump
            ):
                yield f"{module_info.name}.{name}", obj


ALLOWED_PREFIXES = ("watch_", "action_", "on_", "key_", "compose", "render")

# Names we intentionally override as part of building on Textual.
ALLOWED_EXACT = frozenset(
    {
        "compose",
        "render",
        "on_mount",
        "on_unmount",
        "BINDINGS",
        "DEFAULT_CSS",
        "CSS",
        "TITLE",
        "SUB_TITLE",
        "COMMANDS",
        "MODES",
        "AUTO_FOCUS",
        "ENABLE_COMMAND_PALETTE",
        "tick",
        "finish",
        "update",
    }
)


def _declared_names(cls: type) -> set[str]:
    """Names defined on this class, not inherited from Textual bases.

    Textual's metaclass copies framework internals (_computes, _reactives, …)
    into every subclass's ``__dict__``. Treating those as Kalash-defined names
    produces false positives; only names introduced on *this* class matter.
    """
    own = {name for name in vars(cls) if not name.startswith("__")}
    for base in cls.__mro__[1:]:
        if hasattr(base, "__dict__"):
            own -= {name for name in vars(base) if not name.startswith("__")}
    return own


def _init_assigned_names(cls: type) -> set[str]:
    """Names assigned to self inside __init__, found by source inspection."""
    init = vars(cls).get("__init__")
    if init is None:
        return set()
    try:
        source = inspect.getsource(init)
    except (OSError, TypeError):  # pragma: no cover
        return set()
    return set(re.findall(r"self\.(_?[A-Za-z_][A-Za-z0-9_]*)\s*=", source))


@pytest.mark.parametrize("label,cls", list(_kalash_tui_classes()))
def test_no_framework_name_is_shadowed(label: str, cls: type) -> None:
    surface = _framework_surface()
    candidates = (_declared_names(cls) | _init_assigned_names(cls)) - ALLOWED_EXACT
    collisions = sorted(
        name for name in candidates if name in surface and not name.startswith(ALLOWED_PREFIXES)
    )
    assert not collisions, (
        f"{label} defines/assigns {collisions}, which Textual already uses. "
        f"An instance attribute shadows a framework method of the same name, and "
        f"the failure surfaces far from the cause — a shadowed _context broke the "
        f"approval modal's message pump and looked like the app freezing."
    )


def test_the_comprehensive_guard_would_have_caught_context() -> None:
    """Prove the guard detects the real bug it was written for."""
    from textual.screen import ModalScreen

    class Offender(ModalScreen[None]):
        def __init__(self, payload: str) -> None:
            super().__init__()
            self._context = payload  # shadows MessagePump._context

    surface = _framework_surface()
    found = (_declared_names(Offender) | _init_assigned_names(Offender)) - ALLOWED_EXACT
    assert "_context" in found
    assert "_context" in surface


def test_guard_scans_the_whole_tui_package() -> None:
    """A discovery gap would make the parametrized test vacuous."""
    labels = {label for label, _ in _kalash_tui_classes()}
    modules = {label.split(".", 1)[0] for label in labels}
    assert {"app", "messages", "picker", "approval"} <= modules
    assert any("ApprovalScreen" in label for label in labels)
    assert len(labels) >= 5


import re  # noqa: E402
