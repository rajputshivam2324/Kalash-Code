"""Hook discovery from ``.kiro/hooks/*.json`` and ``.kalash/hooks/*.json``.

:class:`~kalash.hooks.runner.HookRunner` could register and dispatch hooks, but
nothing ever read a hook file, so user-defined hooks never existed at runtime.

The file format matches the documented v1 shape::

    {
      "version": "v1",
      "hooks": [{
        "name": "Lint on save",
        "trigger": "PostToolUse",
        "matcher": "write|edit",
        "action": {"type": "command", "command": "npm run lint"}
      }]
    }

Both directories are read so a project can use either convention, project-local
last so it wins. A malformed file is skipped with a warning rather than taking
the session down: a typo in one hook should not stop the agent from starting.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kalash.hooks.events import HookEvent
from kalash.hooks.runner import HandlerType, HookConfig

logger = logging.getLogger(__name__)

HOOK_DIRS: tuple[str, ...] = (".kiro/hooks", ".kalash/hooks")

# Accept both the PascalCase trigger names and lowercase spellings.
_TRIGGERS: dict[str, HookEvent] = {event.value.lower(): event for event in HookEvent}


def _parse_trigger(raw: str) -> HookEvent | None:
    return _TRIGGERS.get(str(raw).strip().lower())


def _parse_hook(raw: dict[str, Any], source: Path) -> HookConfig | None:
    """Build one HookConfig, or None if the record is unusable."""
    trigger = _parse_trigger(raw.get("trigger", ""))
    if trigger is None:
        logger.warning("hook in %s has unknown trigger %r", source, raw.get("trigger"))
        return None

    action = raw.get("action") or {}
    if not isinstance(action, dict):
        return None

    action_type = str(action.get("type", "command")).strip().lower()
    if action_type == "command":
        command = str(action.get("command", "")).strip()
        if not command:
            return None
        return HookConfig(
            name=str(raw.get("name") or source.stem),
            event=trigger,
            handler_type=HandlerType.COMMAND,
            matcher=raw.get("matcher") or None,
            command=command,
            timeout_s=float(raw.get("timeout", 60)),
        )

    if action_type == "http":
        url = str(action.get("url", "")).strip()
        if not url:
            return None
        return HookConfig(
            name=str(raw.get("name") or source.stem),
            event=trigger,
            handler_type=HandlerType.HTTP,
            matcher=raw.get("matcher") or None,
            url=url,
            timeout_s=float(raw.get("timeout", 60)),
        )

    # "agent" actions inject a prompt rather than running anything; they are not
    # executable handlers, so they are ignored here rather than half-supported.
    logger.debug("ignoring %r action in %s", action_type, source)
    return None


def discover_hooks(cwd: Path | None = None) -> list[HookConfig]:
    """Find and parse hook definitions for a project."""
    base = (cwd or Path.cwd()).resolve()
    configs: list[HookConfig] = []

    for relative in HOOK_DIRS:
        directory = base / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("could not read hook file %s: %s", path, exc)
                continue

            entries = payload.get("hooks") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                logger.warning("hook file %s has no 'hooks' array", path)
                continue

            for raw in entries:
                if isinstance(raw, dict) and (config := _parse_hook(raw, path)):
                    config.source_path = str(path.resolve())
                    configs.append(config)

    return configs


def build_hook_runner(cwd: Path | None = None) -> Any | None:
    """Construct a HookRunner with the project's hooks, or None if there are none.

    Returning None when nothing is configured keeps the hot path free of a
    dispatch call that would always be a no-op.
    """
    from kalash.core.trust import is_trusted

    configs = []
    for config in discover_hooks(cwd):
        if is_trusted(Path(config.source_path)):
            configs.append(config)
        else:
            logger.warning(
                "Skipping untrusted hook config %s; authorize with kalash hooks trust",
                config.source_path,
            )
    if not configs:
        return None

    try:
        from kalash.hooks.runner import HookRunner
        from kalash.storage.engine import get_engine

        runner = HookRunner(get_engine())
    except Exception:
        logger.warning("could not initialise the hook runner", exc_info=True)
        return None

    for config in configs:
        runner.register(config)
    logger.debug("registered %d hook(s)", len(configs))
    return runner
