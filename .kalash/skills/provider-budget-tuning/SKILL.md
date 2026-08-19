---
name: provider-budget-tuning
description: Guidelines for calculating token budgets, prompt floors, context compaction, and rate-limit ceilings across model providers.
version: 1.0.0
allowed-tools: [read, edit, shell]
---

# Provider Budget Tuning Skill

Use this skill when adding or tuning LLM providers (Groq, Anthropic, OpenAI, Google, DeepSeek) and rate-limit ceilings.

## 1. Static & Dynamic Limits
- Define context window and output token caps in `src/kalash/models/catalog.py`.
- Handle low TPM providers (e.g. Groq free tier 8k TPM) by dynamically selecting lightweight tool profiles (9 core tools vs 18 full tools).

## 2. Prompt Floor Invariant
- Ensure that `system prompt + tool schemas + user prompt` never exceeds 50% of the model's single-turn token ceiling.
- Use `fold_paths` and observation compaction to compress large outputs (web search, logs) by 80%+.
