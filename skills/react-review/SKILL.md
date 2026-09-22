---
name: react-review
description: Review React components and hooks for correctness and unnecessary rendering when a task asks for React code review, hook cleanup, or behavior-preserving optimization.
---

# React review

Inspect the requested component, its callers, and the repository's public test and lint
configuration before choosing a fix. For an open-ended review, make a short candidate list
with a code location, the observable consequence, and the evidence for each finding.
Distinguish a demonstrated issue from a hypothesis. Prefer high-confidence, local changes;
do not add request cancellation, deduplication, or new interaction behavior merely because
it could be useful.

Check the relevant hooks by tracing their reads and writes:

- State belongs in `useState` when changes must cause rendering. A mutable value used only
  by handlers or imperative callbacks may belong in `useRef`. Check every consumer first:
  changing state to a ref alters rerendering, effect scheduling, and closure behavior. Keep
  state when those semantics matter; do not read or write refs during render as a shortcut.
- Assess `useMemo` by the cost of the calculation and whether consumers need stable identity.
  A cheap primitive expression usually needs no memoization. Objects, arrays, and callbacks
  can have identity-sensitive consumers, so inspect dependencies and callers before removal.
- For effects and callbacks, check stale captures, cleanup, dependencies, and whether a
  derived value can be calculated directly. Do not suppress dependency warnings to hide
  an unresolved lifecycle problem.

Preserve the component's public API, input/output behavior, event order, and existing UI
unless the task asks to change them. Review the final diff against that boundary and remove
unrelated changes. A passing unit test alone does not establish that a hook-quality or
rendering objective has been addressed; explain which code evidence supports that objective.

Run the task's verification commands after the final edit. Where the contract permits,
use the repository's existing public tests, type checks, and lint commands relevant to the
change. If a check cannot run, cite the actual command and observed error, or the explicit
policy restriction that prevented running it. Do not infer that tests are unavailable from
an unrelated command failure. Keep unexecuted checks and unproven findings explicit in the
submission. Do not access hidden evaluators, reference solutions, or change tests to satisfy
the review.

This skill is optional guidance and does not expand the task contract or tool permissions.
