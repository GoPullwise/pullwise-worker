# Evidence-first review method

## Correctness and data integrity

Trace inputs through parsing, normalization, state mutation, persistence, and
publication. Check empty values, missing fields, boundary values, overflow,
partial failure, retry, idempotency, stale versions, and invalid state
transitions. Confirm that durable facts are written atomically and read with the
same invariants.

## Security and authorization

Locate every trust boundary. Verify authentication and authorization at the
side-effect boundary, not only in UI or routing code. Check path containment,
symlink/reparse handling, command operands, secrets, untrusted deserialization,
SSRF, injection, confused-deputy behavior, tenant isolation, and fail-open error
paths.

## Concurrency and reliability

Look for check-then-act races, duplicate work, lost updates, stale leases,
late publication, missing cancellation propagation, incomplete process-tree
cleanup, retry amplification, and recovery that trusts memory over durable
state. Consider crashes between every pair of durable side effects.

## API and contract behavior

Compare producers and consumers, including errors, nullability, ordering,
versions, identifiers, digests, pagination, and terminal outcomes. Treat tests
as evidence of intended behavior, not proof that production matches it.

## Performance

Report only material cliffs supported by a realistic input path: unbounded
memory, accidental quadratic work, repeated full scans, synchronous blocking on
hot paths, or uncontrolled fanout. Avoid micro-optimization advice.

## Findings discipline

A finding needs a concrete trigger, evidence span, observable failure, affected
scope, and practical remediation. Prefer fewer confirmed findings over a long
speculative list. Use `VALIDATED` only when repository evidence actually proves
the trigger; otherwise use `UNVALIDATED` or omit the claim.
