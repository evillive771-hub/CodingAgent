---
name: frontend-debugging
description: Diagnose and verify browser-facing bugs with reproducible evidence
---

# Frontend debugging

Use this guidance when a task concerns browser behavior, UI state, accessibility, rendering, or
frontend data handling.

1. Identify the smallest reproducible user-visible failure and the acceptance boundary.
2. Inspect the relevant component, state transition, event handler, and data normalization path.
3. Prefer a focused failing test or deterministic reproduction before editing.
4. Make the smallest product-code change that addresses the cause, not only the symptom.
5. Run every verification command in the frozen task contract after the last mutation.
6. Record remaining browser, compatibility, accessibility, or regression risks in `submit_result`.

This skill is guidance only. The task contract, host policy, and verification gate remain
authoritative.
