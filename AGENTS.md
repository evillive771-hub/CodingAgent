# Project instructions

- Use Python async interfaces for model, tool, persistence, and external-service I/O.
- Manage dependencies with the project-local uv binary. Keep package caches and virtual environments on D:.
- Never commit `env.txt`, `.env.local`, API keys, access tokens, screenshots containing secrets, or raw authorization headers.
- Keep the model-driven Agent Loop separate from the deterministic delivery workflow.
- Do not expose Git push, Teambition mutation, test deployment, or production release as model tools.
- New tools require a Pydantic input model, risk classification, policy rule, bounded output, and tests.
- External mutations require an idempotency key and an append-only audit event.
- Production release always requires a separate human approval and is outside the Coding Agent.
- Test each module in isolation before wiring it into `DeliveryWorkflow`.
- After integration, run the focused workflow test and then the full pytest/ruff suite.
- Run `uv run ruff check .` and `uv run pytest` before considering a change complete.
