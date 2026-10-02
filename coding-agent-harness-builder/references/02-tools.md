# 02 — Concrete Tool Designs

## Contents
1. Tool design principles
2. Result envelope, truncation, artifacts
3. `read_file`
4. `edit_file` (exact replace)
5. `apply_patch` (patch DSL)
6. `write_file`
7. `bash`
8. `grep`, `glob`, `list_dir`
9. `todo_write`, `ask_user`
10. `spawn_subagent`, web tools
11. Tool descriptions: how to write them
12. Tool tests

---

## 1. Principles

- **Few, sharp tools.** 8–15 total. Every extra tool adds selection errors, especially for weaker models.
- **Dedicated file tools over shell.** They give staleness checks, structured errors, and policy hooks.
- **Pure `describe_effect`.** Each tool declares what it will touch before it runs.
- **Idempotent or explicitly not.** Mark non-idempotent tools; crash recovery treats them differently (see `06` §5).
- **Schemas are strict:** `additionalProperties: false`, required fields minimal, enums over free text, no deeply nested unions (hurts weaker models).
- **Absolute-vs-relative paths:** accept relative to workspace root and absolute inside the workspace; always canonicalize (see `03` §3).

## 2. Result envelope, truncation, artifacts

```python
@dataclass
class ToolOutput:
    text: str
    is_error: bool = False
    artifacts: list[ArtifactRef] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)   # authoritative, from sandbox diff, not from model
    meta: dict = field(default_factory=dict)                  # exit_code, duration_ms, truncated, bytes_total
```

Truncation (applied centrally by the executor, per-tool caps override):

```python
def truncate(text: str, max_bytes=30_000, head=0.4, tail=0.6) -> tuple[str, bool]:
    b = text.encode("utf-8", "replace")
    if len(b) <= max_bytes: return text, False
    h, t = int(max_bytes*head), int(max_bytes*tail)
    omitted = len(b) - h - t
    return (b[:h].decode("utf-8","ignore")
            + f"\n\n… [{omitted} bytes omitted; full output saved as artifact {{artifact_id}}. "
              f"Use read_file with offset/limit or grep on that artifact to inspect] …\n\n"
            + b[-t:].decode("utf-8","ignore")), True
```

Tail-biased for command output (errors are at the end); head-biased for file reads. Also cap *line length* (e.g. 2,000 chars/line) to survive minified JS.

Binary/huge-file guard: detect NUL bytes or >2 MB → refuse with guidance ("binary file; use `bash` with `file`/`xxd` if needed").

## 3. `read_file`

```json
{
  "name": "read_file",
  "input_schema": {
    "type": "object", "additionalProperties": false,
    "properties": {
      "path":   {"type": "string"},
      "offset": {"type": "integer", "minimum": 1, "description": "1-based start line"},
      "limit":  {"type": "integer", "minimum": 1, "maximum": 2000}
    },
    "required": ["path"]
  }
}
```

Behavior:
- Default `limit` 2000 lines; output as `cat -n` style: `{lineno:>6}\t{text}` so the model can cite lines and build patches.
- Records `(path, mtime_ns, sha256, range_read)` in the **file-state cache** (per run). This powers read-before-edit and stale-edit detection.
- Reading the same unchanged file again returns a short "unchanged since last read" stub (saves tokens). Reset on compaction.
- Images (png/jpg) return `Image` blocks if the model supports vision; PDFs/notebooks go through a converter tool, not raw bytes.
- Missing file: suggest nearest matches (difflib on directory listing).

## 4. `edit_file` (exact string replace)

The most important tool; reliability here dominates coding-agent quality.

```json
{
  "name": "edit_file",
  "input_schema": {
    "type": "object", "additionalProperties": false,
    "properties": {
      "path": {"type": "string"},
      "old_string": {"type": "string", "description": "Exact text to replace, including whitespace. Must match exactly once unless replace_all is true."},
      "new_string": {"type": "string"},
      "replace_all": {"type": "boolean", "default": false}
    },
    "required": ["path", "old_string", "new_string"]
  }
}
```

Algorithm:

```python
def edit(path, old, new, replace_all, ctx):
    p = ctx.resolve_writable(path)                          # policy path check
    st = ctx.file_state.get(p)
    if p.exists() and st is None:
        return err("File has not been read in this session. Call read_file first.")
    cur = p.read_bytes(); text = cur.decode("utf-8")        # handle BOM + CRLF: normalise internally, restore on write
    if st and sha256(cur) != st.sha256:
        return err("File changed on disk since you last read it. Re-read it before editing.")
    if old == new: return err("old_string and new_string are identical.")
    n = text.count(old)
    if n == 0:
        return err("old_string not found." + near_miss_hint(text, old))   # show closest block (difflib) + whitespace diagnostics
    if n > 1 and not replace_all:
        return err(f"old_string matches {n} places (lines {match_lines(text, old)[:5]}…). "
                   "Include more surrounding lines to make it unique, or set replace_all.")
    out = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    atomic_write(p, out)                                    # write temp + fsync + rename; preserve mode/EOL
    ctx.file_state.update(p)
    return ok(snippet_with_line_numbers(out, around=first_changed_line(...)) + syntax_check_note(p))
```

Add-ons that measurably help:
- **Fuzzy-assisted errors, not fuzzy edits.** On no-match, report the closest block and the first differing character (tabs vs spaces, trailing whitespace, smart quotes). Never silently apply a fuzzy match unless you evaluate it (Aider-style flexible matching can be offered as an opt-in strategy for weak models).
- **Post-edit diagnostics:** run a fast syntax check (`python -m py_compile`, `tsc --noEmit` on file, `gofmt -e`) via PostToolUse hook and append results.
- **Preserve file metadata:** mode bits, EOL style, trailing newline, encoding.
- **Protect against giant replacements:** warn if `new_string` > 400 lines (suggest `write_file`).

## 5. `apply_patch` (patch DSL, Codex-style)

Useful for multi-hunk/multi-file edits, and models tuned on this format use it fluently. Support both `edit_file` and `apply_patch`; choose per model via config (`edit_strategy: replace | patch | both`).

Grammar:

```text
*** Begin Patch
*** Add File: path/new.py
+line1
+line2
*** Update File: path/existing.py
*** Move to: path/renamed.py          # optional
@@ def func(a, b):                      # context anchor (optional, can repeat)
 context line (leading space)
-removed line
+added line
 context line
*** Delete File: path/old.py
*** End Patch
```

Implementation requirements:
- Parse fully and validate **before** touching disk. Apply all-or-nothing (stage in temp dir, then rename), or roll back from snapshot.
- Locate hunks by context, not line numbers; try exact → rstrip-insensitive → unicode-normalized, and *report which tier matched*.
- Every path goes through the same policy as `edit_file` (workspace-only, protected paths). `Add File` over an existing file = error.
- Return per-file summaries: `A path (+N)`, `M path (+a −b)`, `D path`.
- Errors name the hunk index and show the 3 lines of context that failed to match.
- Compute `changed_paths` from the actual diff.

Test with a corpus of real model-generated patches (collect failures from eval runs and add them as fixtures).

## 6. `write_file`

Create or fully overwrite. Rules: overwrite of an existing file requires prior `read_file` (same staleness check); refuse paths outside the workspace; create parent dirs; atomic write; return line count. Nudge via description: "Prefer edit_file for modifications."

## 7. `bash`

```json
{
  "name": "bash",
  "input_schema": {
    "type": "object", "additionalProperties": false,
    "properties": {
      "command": {"type": "string"},
      "description": {"type": "string", "description": "5–10 word summary shown in approval prompts"},
      "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 600000},
      "run_in_background": {"type": "boolean", "default": false}
    },
    "required": ["command"]
  }
}
```

Design decisions:

| Question | Recommendation |
|---|---|
| Persistent shell or fresh per call? | Fresh process per call by default (robust, simple to sandbox, easy to kill). Persist **cwd and exported env** across calls by capturing them after each command (`pwd -P; env -0` to a side channel) and reapplying. Persistent REPL shells hang and leak state; avoid unless needed. |
| Timeout | Default 120 s, max 600 s, **enforced by the runtime** at process-group level. |
| Output | Stream to a rotating file; return truncated tail-biased text + exit code + duration. Interleave stdout/stderr with labels only if the model needs them. |
| Stdin | Closed (`/dev/null`). Interactive prompts fail fast instead of hanging. Set `CI=1`, `GIT_TERMINAL_PROMPT=0`, `PAGER=cat`, `DEBIAN_FRONTEND=noninteractive`, `NO_COLOR=1`. |
| Background | `run_in_background` returns a handle; companion tools `bash_output(handle, since)` and `bash_kill(handle)`. Needed for dev servers/watchers. Cap count (e.g. 3) and kill all at run end. |
| Env | Start from an allowlist (`PATH`, `HOME` (sandbox), `LANG`, `TERM`, project-specific). Never inherit host env wholesale. |
| cwd | Workspace root unless `cd` earlier; reject cwd outside workspace. |
| Changed files | Compute with sandbox-level diff (git status or overlay diff), not by parsing the command. |

Process handling (Python):

```python
proc = await asyncio.create_subprocess_exec(*argv, stdin=DEVNULL, stdout=PIPE, stderr=STDOUT,
                                            cwd=cwd, env=env, start_new_session=True)   # new process group
try:
    out = await asyncio.wait_for(read_capped(proc.stdout, hard_cap=50_000_000), timeout)
except asyncio.TimeoutError:
    os.killpg(proc.pid, signal.SIGTERM); await asyncio.sleep(2); os.killpg(proc.pid, signal.SIGKILL)
```

Always wrap in the sandbox (`bwrap … -- bash -lc "$cmd"` or container exec). `bash` is the highest-risk tool; its `describe_effect` returns the *parsed* argv list(s) (see `03` §2), not the raw string.

## 8. `grep`, `glob`, `list_dir`

`grep` wraps ripgrep with a fixed argv template (never string-concatenate user input into a shell):

```json
{"pattern": "str", "path": "str?", "glob": "str?", "type": "str?",
 "output_mode": "files_with_matches|content|count", "context": "int?", "case_insensitive": "bool?",
 "multiline": "bool?", "head_limit": "int? (default 250)"}
```
- Respect `.gitignore` by default; add explicit `include_ignored` flag.
- Default `files_with_matches` → cheap discovery, then `content` for the few files that matter.
- Cap results and say how many were omitted.
- Sort by mtime for `glob` (recently edited files are more relevant).

`list_dir`: depth-limited (default 2), ignore `.git`, `node_modules`, build dirs; show sizes; truncate with counts.

Prefer agentic search (`grep`/`glob`/read) over embedding retrieval first; it is robust, always fresh, and what the leading coding agents ship. Add a symbol index or repo map only if evals show a lift.

## 9. `todo_write`, `ask_user`

- `todo_write(todos: [{id, content, status: pending|in_progress|completed}])` replaces the whole list. Cheap, effective at keeping long tasks on-track; harness re-injects the current list after compaction and surfaces it in the UI. Completion gate can require none pending.
- `ask_user(question, options?)` pauses the run (`PAUSED_FOR_USER`), persists, and resumes on answer. In headless mode it returns a configured default or fails the run, never blocks forever.

## 10. `spawn_subagent`, web tools

- `spawn_subagent(task, agent_type, context_files?)`: see `09-extensibility.md` §1.
- `web_fetch(url, prompt?)`: HTTP via the **egress proxy** with allowlist; strip scripts/styles, convert to markdown, cap size; mark result `untrusted`; `Risk.NETWORK` → ask by default.
- `web_search(query)`: provider API; results are untrusted data.

## 11. Writing tool descriptions

Descriptions are prompts. Include: what it does, when to prefer it, when *not* to use it, constraints, one example. Example for `edit_file`:

> Replace exact text in an existing file. You must `read_file` the file first. `old_string` must match exactly once (include enough surrounding lines to be unique) unless `replace_all` is true. Preserve indentation exactly as shown after the line-number tab. Use `write_file` only for new files or complete rewrites.

Treat descriptions as versioned artifacts: hash them into the run's `config_fingerprint`; change them only with an eval run (tiny wording changes shift tool-use rates).

## 12. Tool tests (minimum)

- Schema fuzz: random/invalid args never crash the executor (always a `ToolResult`).
- `edit_file`: 0 / 1 / many matches, CRLF files, BOM, tabs vs spaces, stale file, unread file, binary file, symlink target, read-only file, unicode.
- `apply_patch`: valid multi-file, context drift, partial failure rolls back, path escape, add-over-existing, EOF without newline.
- `bash`: timeout kills grandchildren (`sleep` in subshell), huge output capped, stdin closed, env scrubbed, background cleanup at run end, cancel mid-run.
- Truncation: head/tail correctness, UTF-8 boundary safety, artifact stored and retrievable.
- Property: for any sequence of tool calls, workspace stays inside the allowed root (checked with a filesystem watcher or post-run diff of the host outside workspace).
