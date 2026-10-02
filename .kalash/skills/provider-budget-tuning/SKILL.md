---
name: provider-budget-tuning
description: Diagnose model capability errors and configure explicit run budgets without reducing harness quality.
---

# Provider and run limits

- Use the provider's actual context window and output capability.
- Keep the full permitted tool set and complete system instructions.
- A tokens-per-minute quota error is a provider/account limit. Do not shorten the prompt or downgrade reasoning to hide it.
- Retry transient rate limits with bounded backoff; report permanent quota failures clearly.
- Configure tokens, cost, attempted tools, model turns and elapsed time as run ceilings.
- Charge summary calls and child usage to the same run.
- Check input, cached input, output and reasoning usage; record unknown prices honestly.
- Compact closed conversation batches only near the context limit. Preserve user constraints, active skills and scoped guidance.
