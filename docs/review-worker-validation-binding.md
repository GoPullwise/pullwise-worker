# Review Worker Validation Binding Plan

## Goal

Move main review findings from prompt-only discipline to a mechanical worker
contract.

Final contract:

`report.agent.json.findings` contains only main findings backed by
`validated-findings.json`. Findings without confirmed, plausible, or validated
validation output may appear only in appendix data. QA must fail any non-empty
main finding list that contains an unbacked finding.

## Current Gap

The worker already has strong artifact reliability checks: phase outputs,
schemas, artifact manifest shape, SHA-256, size, storage URL, source file
immutability, pending result submit handling, and terminal envelope structure.

Finding precision is improved by reviewer, clusterer, intent test, validator,
and reporter stages, but those are mostly prompt-driven semantic controls. The
validator disproof prompt is the most important semantic filter, yet the final
QA gate currently validates only the final report's structure and evidence
shape: required fields, locations, confidence, and intent/validator-status
shape. It does not prove semantic truth, and it does not mechanically require
each main finding to come from `validated-findings.json`.

This means a reporter turn can accidentally place weak, disproven, or unrelated
content in `report.agent.json.findings` if the fields and locations are
otherwise valid.

## Scope

Affected project: `pullwise-worker`.

Primary files:

- `pullwise_worker/review_worker_v1.py`
- `tests/test_review_worker_v1.py`
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

Rejected statuses include:

- `weak`
- `disproven`
- `rejected`
- `false_positive`
- empty or unknown values

Finding id aliases:

- `id`
- `finding_id`
- `cluster_id`
- `local_id`
- `source_finding_id`
- `source_finding_ids`

`source_finding_ids` may be a scalar or a list. Empty values are ignored.

## Matching Strategy

Prefer id matching. Collect all id aliases from the report finding and each
validation entry. A non-empty intersection with an accepted validation entry is
a successful binding.

Fallback matching is allowed only when the report finding has no usable id.
Fallback key:

`title + primary path + start_line`

Fallback requirements:

- Use fallback only when the report finding has no id aliases.
- The validation entry must expose the same title, primary path, and start line.
- The match must be unique across accepted validation entries.
- Zero matches means unbacked.
- Multiple matches means ambiguous and therefore unbacked.

Primary path and line may be read from common shapes such as:

- `locations[0].path`
- `location.path`
- `path`
- `file`
- `primaryFile`
- `start_line`
- `line`
- `line_start`
- `primaryLine`

## Report Repair Behavior

`repair_agent_report_artifact(...)` must normalize the report, then enforce the
binding contract.

For each normalized main finding:

- If backed by accepted validation output, keep it in `findings`.
- If unbacked, move it to `appendix_findings`.

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
- Every main finding must bind to an accepted validation entry.

If main findings are empty, empty or missing accepted validation findings must
not fail QA by itself. A completed scan with no confirmed findings is a valid
result and the markdown report must continue to say this is not proof that the
repository has no defects.

Stable QA errors:

- `validated-findings.json is missing or invalid for non-empty main findings`
- `finding[{index}] is not backed by confirmed/plausible validation`

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

Add focused tests in `tests/test_review_worker_v1.py`:

- `test_repair_agent_report_demotes_unvalidated_main_findings`
- `test_qa_gate_rejects_main_finding_not_in_validated_findings`
- `test_qa_gate_accepts_main_finding_backed_by_plausible_validation`
- `test_qa_gate_allows_empty_main_findings_with_empty_validation`
- `test_validation_binding_supports_cluster_id_alias`
- `test_validation_binding_fallback_requires_unique_match`

The tests must cover:

- confirmed/plausible/validated accepted statuses
- weak/disproven/rejected/false_positive rejected statuses
- id aliases on both report and validation entries
- unique fallback matching only when the report finding has no id
- ambiguous fallback is rejected
- demoted findings move to appendix and keep demotion metadata
- `summary.overall_risk` is recomputed from retained main findings
- `next_agent_tasks` is rebuilt from retained main findings only

## Verification

Run:

```bash
python -m unittest tests.test_review_worker_v1
```

At the time this plan was written, the current worker test file contains 169
tests and has an unrelated existing failure in repository limit stats:
`test_prepare_workspace_reports_full_repository_stats_when_limit_exceeded`
expects `totalBytes == 9` but currently observes `12`. Fix that baseline issue
as part of bringing this work to green verification.

