#!/usr/bin/env python3
"""Offline incident-triage CLI.

Persist incidents to a JSON file. Commands:
  add TITLE [--severity critical|high|medium|low]
  list [--status all|open|closed] [--severity critical|high|medium|low]
  close ID
  summary
  --help
"""

import argparse
import json
import os
import sys
import tempfile
from typing import Any

VALID_SEVERITIES = ("critical", "high", "medium", "low")
VALID_STATUSES = ("open", "closed")


def load_store(path: str) -> list:
    """Read JSON store; return empty list if missing or invalid."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return data


def save_store(path: str, incidents: list) -> None:
    """Atomically write incidents to path using a temp file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=parent, delete=False, prefix=".triage_tmp_"
    )
    try:
        json.dump(incidents, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        os.replace(tmp.name, path)
    except Exception:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        raise


def ensure_schema(incidents: list) -> list:
    """Strip/repair a list of incident dicts to the required schema."""
    out = []
    for item in incidents:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "id": int(item.get("id", 0)),
                "title": str(item.get("title", "")).strip(),
                "severity": str(item.get("severity", "medium")).strip().lower(),
                "status": str(item.get("status", "open")).strip().lower(),
            }
        )
    return out


def next_id(incidents: list) -> int:
    if not incidents:
        return 1
    return max(inc["id"] for inc in incidents) + 1


def validate_severity(sev: str) -> str:
    if sev.strip().lower() not in VALID_SEVERITIES:
        raise ValueError(f"invalid severity: {sev!r}")
    return sev.strip().lower()


def validate_status(status: str) -> str:
    if status.strip().lower() not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    return status.strip().lower()


def cmd_add(args: argparse.Namespace, store_path: str) -> None:
    title = args.title
    if not title or not title.strip():
        print(
            json.dumps({"error": "title must be a non-empty string"}),
            file=sys.stderr,
        )
        sys.exit(1)
    title = title.strip()
    severity = validate_severity(args.severity)
    incidents = load_store(store_path)
    incidents = ensure_schema(incidents)
    incidents.append(
        {
            "id": next_id(incidents),
            "title": title,
            "severity": severity,
            "status": "open",
        }
    )
    save_store(store_path, incidents)
    print(json.dumps({"id": incidents[-1]["id"], "title": incidents[-1]["title"], "severity": incidents[-1]["severity"], "status": incidents[-1]["status"]}))


def cmd_list(args: argparse.Namespace, store_path: str) -> None:
    incidents = load_store(store_path)
    incidents = ensure_schema(incidents)
    status_filter = args.status.lower()
    severity_filter = args.severity.lower() if args.severity else None
    out = []
    for inc in incidents:
        if status_filter != "all" and inc["status"] != status_filter:
            continue
        if severity_filter and inc["severity"] != severity_filter:
            continue
        out.append(inc)
    # Sort: severity critical > high > medium > low, then ascending id
    sev_order = {s: i for i, s in enumerate(VALID_SEVERITIES)}
    out.sort(key=lambda x: (sev_order[x["severity"]], x["id"]))
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_close(args: argparse.Namespace, store_path: str) -> None:
    incidents = load_store(store_path)
    incidents = ensure_schema(incidents)
    target_id = int(args.id)
    for inc in incidents:
        if inc["id"] == target_id:
            inc["status"] = "closed"
            save_store(store_path, incidents)
            print(json.dumps({"id": inc["id"], "title": inc["title"], "severity": inc["severity"], "status": inc["status"]}))
            return
    print(
        json.dumps({"error": f"incident id {target_id} not found"}),
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_summary(args: argparse.Namespace, store_path: str) -> None:
    incidents = load_store(store_path)
    incidents = ensure_schema(incidents)
    open_count = sum(1 for inc in incidents if inc["status"] == "open")
    closed_count = sum(1 for inc in incidents if inc["status"] == "closed")
    by_severity = {
        "critical": sum(1 for inc in incidents if inc["severity"] == "critical" and inc["status"] == "open"),
        "high": sum(1 for inc in incidents if inc["severity"] == "high" and inc["status"] == "open"),
        "medium": sum(1 for inc in incidents if inc["severity"] == "medium" and inc["status"] == "open"),
        "low": sum(1 for inc in incidents if inc["severity"] == "low" and inc["status"] == "open"),
    }
    print(json.dumps({"open": open_count, "closed": closed_count, "by_severity": by_severity}))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Offline incident triage CLI",
        add_help=False,
    )
    p.add_argument("--help", action="store_true", help="Show this help message and exit")
    p.add_argument("--store", default="incidents.json", help="Path to JSON store (default: incidents.json)")
    p.add_argument("command", nargs="?", default="help", help="Command to run")
    sub = p.add_subparsers(dest="subcommand", required=False)

    add_p = sub.add_parser("add", help="Add an incident")
    add_p.add_argument("title", nargs="?", default="", help="Incident title")
    add_p.add_argument("--severity", default="medium", help="Severity: critical|high|medium|low (default: medium)")
    add_p.set_defaults(func=lambda a, s: cmd_add(a, s))

    list_p = sub.add_parser("list", help="List incidents")
    list_p.add_argument("--status", default="all", choices=["all", "open", "closed"], help="Filter by status (default: all)")
    list_p.add_argument("--severity", default=None, choices=["critical", "high", "medium", "low"], help="Filter by severity")
    list_p.set_defaults(func=lambda a, s: cmd_list(a, s))

    close_p = sub.add_parser("close", help="Close an incident")
    close_p.add_argument("id", nargs="?", default="", help="Incident id to close")
    close_p.set_defaults(func=lambda a, s: cmd_close(a, s))

    summary_p = sub.add_parser("summary", help="Show summary counts")
    summary_p.set_defaults(func=lambda a, s: cmd_summary(a, s))

    return p


def main(argv: list[str] | None = None) -> None:
    argv = argv or sys.argv[1:]
    if not argv:
        argv = ["--help"]
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        if e.code != 0:
            raise
        return
    if args.help:
        parser.print_help()
        return
    if args.command == "help":
        parser.print_help()
        return
    if args.subcommand is None:
        print("No command specified. Run with --help for usage.", file=sys.stderr)
        sys.exit(1)
    args.func(args, args.store)


if __name__ == "__main__":
    main()
