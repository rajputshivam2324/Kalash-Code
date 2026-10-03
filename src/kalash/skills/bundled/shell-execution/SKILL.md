---
name: shell-execution
description: Run builds, tests, commands and background processes under the existing sandbox.
version: 1.0.0
---

# shell-execution

Use the shell tool for execution; its permissions, timeout and sandbox are authoritative.
Prefer small commands with explicit cwd. Batch only independent reads; sequence changes
and verification. Narrow verbose results or retrieve deferred output with expand.
Use the shell background handle for long services, inspect output, and terminate when done.
A rejected command is feedback; do not change execution paths to bypass the gate.
