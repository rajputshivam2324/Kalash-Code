"""Parameterized scope and recall queries. Scope filtering precedes LIMIT."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from kalash.memory.protocol import RecallQuery, Scope


def scope_filter(scope: Scope, prefix: str = "r.", *, exact: bool = False) -> tuple[str, list[Any]]:
    p = prefix
    if exact:
        conditions = [f"{p}scope_user_id = ?", f"{p}scope_visibility = ?"]
        params: list[Any] = [scope.user_id, scope.visibility.value]
        if scope.visibility != "global":
            if not scope.project_id:
                return "0", []
            conditions.append(f"{p}scope_project_id = ?")
            params.append(scope.project_id)
        for visibility, identifier in (("session", scope.session_id), ("agent", scope.agent_id)):
            if scope.visibility == visibility:
                if not identifier:
                    return "0", []
                conditions.append(f"{p}scope_{visibility}_id = ?")
                params.append(identifier)
        return " AND ".join(conditions), params
    return (
        f"""{p}scope_user_id = ? AND ({p}scope_visibility = 'global' OR
        ({p}scope_project_id = ? AND ? IS NOT NULL AND (
            {p}scope_visibility = 'project' OR
            ({p}scope_visibility = 'session' AND {p}scope_session_id = ? AND ? IS NOT NULL) OR
            ({p}scope_visibility = 'agent' AND {p}scope_agent_id = ? AND ? IS NOT NULL))))""",
        [
            scope.user_id,
            scope.project_id,
            scope.project_id,
            scope.session_id,
            scope.session_id,
            scope.agent_id,
            scope.agent_id,
        ],
    )


def recall_sql(query: RecallQuery) -> tuple[str | None, tuple[Any, ...]]:
    if query.limit <= 0:
        return None, ()
    conditions = ["r.tombstoned = 0"]
    params: list[Any] = []
    if query.scope:
        sql, values = scope_filter(query.scope)
        conditions.append(sql)
        params.extend(values)
    if query.kinds:
        conditions.append("r.kind IN (" + ",".join("?" for _ in query.kinds) + ")")
        params.extend(kind.value for kind in query.kinds)
    for column, value, operator in (
        ("confidence", query.min_confidence, ">="),
        ("salience", query.min_salience, ">="),
    ):
        conditions.append(f"CAST(r.{column} AS REAL) {operator} ?")
        params.append(float(value))
    for column, text_value, operator in (
        ("subject_key", query.subject_key, "="),
        ("prov_created_at", query.after.isoformat() if query.after else None, ">="),
        ("prov_created_at", query.before.isoformat() if query.before else None, "<="),
    ):
        if text_value is not None:
            conditions.append(f"r.{column} {operator} ?")
            params.append(text_value)
    if not query.include_superseded:
        conditions.append("r.superseded_by IS NULL")
    if not query.include_expired:
        conditions.append("(r.expires_at IS NULL OR r.expires_at > ?)")
        params.append(datetime.now(UTC).isoformat())
    where = " AND ".join(conditions)
    if query.text:
        tokens = re.findall(r"[^\W_]+", query.text, flags=re.UNICODE)[:10]
        if not tokens:
            return None, ()
        match = " OR ".join(f'"{token}"' for token in tokens)
        return (
            f"SELECT r.*, fts.rank AS fts_rank FROM memory_fts fts JOIN memory_records r ON r.id = fts.id WHERE memory_fts MATCH ? AND {where} ORDER BY fts.rank LIMIT ?",
            (match, *params, query.limit),
        )
    return (
        f"SELECT r.*, 0 AS fts_rank FROM memory_records r WHERE {where} ORDER BY CAST(r.salience AS REAL) DESC, r.created_at DESC LIMIT ?",
        (*params, query.limit),
    )
