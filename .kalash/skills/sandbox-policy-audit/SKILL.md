---
name: sandbox-policy-audit
description: Rules and testing methods for auditing tool security, command classification, and bubblewrap sandbox isolation.
version: 1.0.0
allowed-tools: [read, shell, search]
---

# Sandbox & Policy Audit Skill

Use this skill when auditing tool permissions or modifying execution safety rules.

## 1. Permission Risk Classes
- **Safe / Read**: `read`, `glob`, `list`, `search`, `recall` -> No approval required in standard mode.
- **Write**: `write`, `edit`, `multi_edit` inside workspace -> Auto-approved or prompt depending on config.
- **Exec / Shell**: Arbitrary shell commands -> Strict regex/AST classification into benign vs dangerous.
- **Danger**: `rm -rf /`, piping unverified scripts to bash, modifying `/etc` or user home credentials -> Always DENY or require explicit approval.

## 2. Verifying Safety Suite
```bash
uv run pytest tests/unit/test_permissions.py
uv run pytest tests/unit/test_sandbox.py
```
