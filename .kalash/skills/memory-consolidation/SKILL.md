---
name: memory-consolidation
description: Procedures for extracting, indexing, and recalling episodic facts, project rules, and developer preferences in SQLite.
version: 1.0.0
allowed-tools: [read, recall, remember, forget, shell]
---

# Memory Consolidation Skill

Use this skill when working on long-term memory capture, semantic recall, or memory provider extensions.

## 1. Capture Lifecycle
1. Turn Completion: Agent analyzes conversation turn for actionable facts (e.g. "User prefers pytest -q", "Project uses hatchling build backend").
2. Memory Distillation: Store clean key-value / semantic statements in SQLite FTS5 table.
3. Turn Recall: At the start of a new turn, query SQLite using BM25 / FTS5 ranking and inject top results into system context within the 8% recall token budget.
