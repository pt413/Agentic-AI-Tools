# Lead management capability split

This folder contains the lead review/rating/dashboard implementation.

## Files

- `common.py` — shared constants, DB helpers, text/phone/time helpers, role resolver.
- `cleaning.py` — output cleaning for raw/evidence/LLM modes.
- `evidence.py` — lead-scoped evidence collection from staging tables.
- `summary.py` — phase, call, stakeholder and lead-effectiveness summaries.
- `prompt.py` — lead-review prompt, CLI payload rendering helpers.
- `llm_client.py` — upstream LLM proxy call and stable context hash helpers.
- `parsing.py` — parse LLM score/action/stakeholder/actor tables.
- `cache.py` — `lead_communication_review` table, one-row-per-lead cache, indexes.
- `rating_runner.py` — cache-first rating runner used by `/llm-rating`.
- `dashboard.py` — dashboard list query; never calls the LLM.
- `jobs.py` — stale marking and recompute helpers for cron jobs.

`../review_lead_communication.py` remains a compatibility wrapper so existing imports/routes keep working.
