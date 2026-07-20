# Review Worker Validation Binding Contract

Status: synchronized with the current implementation on 2026-07-20.

## Goal

Move main review findings from prompt-only discipline to a mechanical worker
contract.

Final contract:

`report.agent.json.findings` contains only main findings backed by
`validated-findings.json`. Findings without confirmed, plausible, or validated
validation output may appear only in appendix data. QA must fail any non-empty
main finding list that contains an unbacked finding.

## Implementation Status

The worker already has strong artifact reliability checks: phase outputs,
schemas, artifact manifest shape, SHA-256, size, storage URL, source file
immutability, pending result submit handling, and terminal envelope structure.

The former mechanical gap is closed in `repair_agent_report_artifact(...)`,
`validation_binding_entries(...)`, `matching_validation_entry(...)`, and
`qa_gate_payload(...)`. Prompt-driven reviewer and validator judgments remain
semantic evidence rather than proof of truth, but an accepted
`validated-findings.json` entry is now required before a finding can remain in
the main report. Unbacked, weak, disproven, rejected, false-positive, unrelated,
or ambiguously matched content is demoted or rejected even when its report
shape and location are otherwise valid.

The main-report/validator relation is one-to-one at the QA boundary. A main
finding must have exactly one accepted validator entry, one validator entry may
not back multiple main findings, and every accepted validator entry must appear
in the main report. Report repair enforces the no-reuse direction by demoting a
second finding that resolves to an already-used entry; QA enforces both
directions.

## Scope

Affected project: `pullwise-worker`.

Primary files:

- `pullwise_worker/review_worker_v1.py`
- `tests/test_review_worker_v1.py`
- `tests/test_result_truthfulness_regressions.py`
- `AGENTS.md`

This work does not change server protocol fields, the legacy top-level
`summary`, or the legacy top-level `agentReport`.

## Binding Rules

Each main `report.agent.json.findings[]` item must match an entry in
`validated-findings.json.validated_findings[]` whose status is one of:

- `confirmed`
- `plausible`
- `validated`

Accepted validation status field aliases:

- `status`
- `validator_status`
- `validation_status`
- `classification`
- `disposition`

The implementation checks these fields in the listed order, takes the first
non-empty value, then trims and lowercases it.

Non-backing weak statuses are:

- `weak`
- `suppressed`
- `unresolved`
- `appendix`

Non-backing disproven statuses are:

- `disproven`
- `rejected`
- `false_positive`
- `invalid`

Empty or unknown statuses are also non-backing. Strict validation reports them
as unsupported dispositions rather than silently treating them as confirmed.

Finding id aliases:

- `id`
- `finding_id`
- `finding_ids`
- `cluster_id`
- `source_cluster_id`
- `candidate_id`
- `canonical_finding_id`
- `local_id`
- `source_finding_id`
- `source_finding_ids`

Every ID alias is accepted as a scalar or collection; nested collection values
are flattened by the shared collector. Empty values are ignored. The same
alias set is applied to report findings and validator entries.

## Matching Strategy

Prefer ID matching. Collect all ID aliases from the report finding and each
accepted validation entry.

- Exactly one accepted entry with a non-empty ID intersection is a successful
  binding.
- More than one ID match is ambiguous and fails binding; do not fall back.
- Zero ID matches proceeds to the fallback key. This remains true when the
  report and validator records both have usable IDs but those IDs do not
  intersect, which covers model-local ID drift.

Fallback key:

`title + primary path + start_line`

Fallback requirements:

- Use fallback when there is no ID match, whether the report has no usable ID
  or has non-matching model-local IDs.
- The validation entry must expose the same title, primary path, and start line.
- The match must be unique across accepted validation entries.
- Zero matches means unbacked.
- Multiple matches means ambiguous and therefore unbacked.

Titles are trimmed, lowercased, and whitespace-collapsed. Paths normalize
backslashes to forward slashes and remove leading `./` segments.

Primary path and line may be read from common shapes such as:

- `locations[0].path`
- `location.path`
- `path`
- `file`
- `primaryFile`
- `primary_file`
- `start_line`
- `line`
- `line_start`
- `primaryLine`
- `primary_line`
- `startLine`
- `lineStart`

## Report Repair Behavior

`repair_agent_report_artifact(...)` must normalize the report, then enforce the
binding contract.

For each normalized main finding:

- If it resolves to one accepted, not-yet-used validator entry, keep it in
  `findings`, mark that entry used, and copy the normalized accepted status to
  `validator_status`.
- If it is unbacked, ambiguous, or resolves to an entry already used by an
  earlier main finding, move it to `appendix_findings`.

Repair does not synthesize a report finding for an unmatched accepted validator
entry. The later QA reverse-coverage check rejects that mismatch.

Demoted findings must include:

- `demoted_from_main_findings: true`
- `demoted_reason: "missing_confirmed_or_plausible_validation"`

`summary.overall_risk` must be recomputed from the retained main findings every
time. Do not preserve a model-supplied non-`unknown` risk after demotion.

`next_agent_tasks` must be rebuilt only from retained main findings. Do not
preserve existing report-level tasks unless they can be derived from retained
main findings.

## QA Behavior

`qa_gate_payload(...)` must keep existing checks and add a final backing check.

If main findings are non-empty:

- `validated-findings.json` must exist.
- It must be a JSON object.
- `schema_version` must be `validation-output/v1`.
- `validated_findings` must be a list.
- Every main finding must bind to exactly one accepted validation entry.
- No accepted validator entry may be reused by another main finding.

Whenever the validation artifact is structurally valid, every accepted
validator entry must also be matched by a main report finding. This reverse
check applies even when `report.agent.json.findings` is empty.

An empty main report with a missing validation artifact or with a valid artifact
whose accepted list is empty does not fail this binding check by itself. A
completed scan with no accepted findings is a valid result, and the Markdown
report must continue to say this is not proof that the repository has no
defects.

Stable QA errors:

- `validated-findings.json is missing or invalid for non-empty main findings`
- `finding[{index}] is not backed by confirmed/plausible validation`
- `finding[{index}] reuses validation evidence already bound to another main finding`
- `validated main finding {label} is missing from report.agent.json`

For the reverse-coverage error, `{label}` is the entry's first usable stable ID
according to the implementation's label order, or `validation[{index}]` when no
usable ID exists. The existing confirmed/plausible error text remains stable
even though `validated` is also an accepted status.

## Location Rules

Do not require reviewer-stage invalid locations to be zero. Reviewer outputs are
candidate evidence. Later semantic stages may discard or refine them.

The final main findings still must pass existing QA location checks: path is
inside the repository, file exists, and line range is valid.

## Fallback Behavior

Keep existing fallback behavior for degraded semantic phases, including an empty
`validated_findings` fallback for `validator_disproof`.

The new contract means fallback can reduce recall or coverage completeness, but
it must not allow unvalidated findings into the main report:

- Repair should demote unbacked findings.
- QA should fail any unbacked finding that remains in the main list.

## Tests

Focused repair and matching coverage in `tests/test_review_worker_v1.py`
includes:

- `test_report_repair_binds_report_source_ids_to_validator_ids`
- `test_repair_agent_report_demotes_unvalidated_main_findings`
- `test_repair_agent_report_uses_location_binding_when_model_ids_differ`
- `test_validation_binding_supports_id_and_status_aliases`
- `test_validation_binding_supports_cluster_id_alias`
- `test_validation_binding_fallback_accepts_unique_match_without_report_id`
- `test_validation_binding_fallback_accepts_unique_match_when_model_ids_differ`
- `test_validation_binding_fallback_requires_unique_match`

Focused QA coverage in the same file includes:

- `test_qa_gate_rejects_non_empty_main_findings_when_validation_artifact_missing`
- `test_qa_gate_rejects_main_finding_not_in_validated_findings`
- `test_qa_gate_rejects_all_non_backing_validation_statuses`
- `test_qa_gate_accepts_main_finding_backed_by_plausible_validation`
- `test_qa_gate_accepts_main_finding_backed_by_confirmed_disposition`
- `test_qa_gate_accepts_location_binding_when_model_ids_differ`
- `test_qa_gate_allows_empty_main_findings_with_empty_validation`
- `test_qa_gate_rejects_validated_main_findings_missing_from_report`

`tests/test_result_truthfulness_regressions.py` adds
`test_one_validator_entry_cannot_back_multiple_main_findings`, which verifies
that report repair retains one main finding and demotes a second finding trying
to reuse the same validator entry.

Together these tests exercise representative ID/status aliases on both sides,
the `disposition` status alias, unique fallback both without an ID and with
non-intersecting model IDs, ambiguous fallback rejection, reverse coverage,
demotion metadata, risk recomputation, and rebuilding `next_agent_tasks` from
retained main findings. `FINDING_ID_ALIAS_FIELDS` and
`VALIDATION_STATUS_ALIAS_FIELDS` in the implementation remain the authoritative
complete alias registries.

## Verification

Run:

```bash
python -m unittest tests.test_review_worker_v1
python -m unittest tests.test_result_truthfulness_regressions
```

This contract document intentionally does not pin a historical test count or
pass/skip total. Use the current command output and CI run as the authoritative
verification evidence; synchronizing this document does not imply that either
suite was rerun.
