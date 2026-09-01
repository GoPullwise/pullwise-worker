---
name: pullwise-repository-review
description: Perform an evidence-backed, read-only repository review and return the strict Pullwise payload.
---

# Pullwise repository review

Use the Worker-provided review reference as the method. Work from repository
evidence, not intuition. The review is read-only.

1. Establish the repository shape, languages, entry points, trust boundaries,
   state transitions, persistence boundaries, and test strategy.
2. Follow high-risk flows across definitions and callers. Prioritize correctness,
   security, authorization, concurrency, data integrity, reliability, API
   contracts, performance cliffs, and missing regression tests.
3. Try to disprove each suspected issue. Do not report style preferences,
   speculative risks, or claims without a concrete triggering path.
4. For every confirmed issue, provide a repository-relative byte span, exact
   evidence text, impact, remediation, stable fingerprint, dense ordinal, and
   honest validation status.
5. Record inspected paths in coverage. Use a non-REVIEWED state and an allowed
   reason when a path could not be assessed.
6. Return only the JSON payload defined by the attempt prompt. Never add an
   envelope, usage, prose outside JSON, or Markdown fencing.
