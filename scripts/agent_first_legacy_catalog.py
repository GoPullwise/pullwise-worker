"""Immutable ceiling catalog for the D27 legacy-absence ratchet."""

EXPECTED_INVENTORY_ID = "agent-first-clean-break-d27-legacy-removal-2026-07-20"
EXPECTED_D27 = {
    "register_path": "contracts/agent-first/spec-decision-register.json",
    "decision_id": "D27",
    "selected_option_id": "clean_break_no_legacy",
    "resolution_sha256": "f3ef27ad6318d4da20d4750cdde9387b66045f1708a909b57aba1c6e48ec2b0e",
}
EXPECTED_FROZEN_BASELINE = {
    "path": "contracts/agent-first/legacy-v1-contract-baseline.json",
    "baseline_id": "legacy-v1-server-web-2026-07-17",
    "text_sha256": "16b564b52cfa14e7504cd71af382fde1ff6b35e71ed85f91e722b0ccf450f6fd",
    "surface_ids": [
        "server.artifact-event-wire",
        "server.cancellation-fixtures",
        "server.claim-policy-source",
        "server.claim-result-projection",
        "server.durable-protocol-storage",
        "server.policy-fixtures",
        "server.progress-debug-projection",
        "server.result-fixtures",
        "server.route-fixtures",
        "server.status-projection",
        "server.system-limit-fixtures",
        "server.system-limits",
        "web.api-consumer",
        "web.api-fixtures",
        "web.flow-fixtures",
        "web.flow-projection",
        "web.history-fixtures",
        "web.history-projection",
        "web.normalizer-consumer",
        "web.normalizer-fixtures",
        "web.progress-fixtures",
        "web.progress-projection",
        "web.timing-fixtures",
        "web.timing-projection",
        "worker.public-scan-canonical-fixture",
        "worker.public-scan-fixture-validator",
        "worker.strict-v1-wire-canonical-fixture",
        "worker.strict-v1-wire-fixture-validator",
    ],
}
CATALOG_FIELDS = (
    "d27",
    "frozen_baseline",
    "signatures",
    "evidence_exclusions",
    "surfaces",
)
EXPECTED_CATALOG_SHA256 = "a3f4a6b32eb66dd33e0855743bbbcdc024c2ce760f4aaf56683f495a7312be68"

ALLOWED_WHOLE_EXCLUSIONS = {
    (
        "worker",
        "contracts/agent-first/legacy-removal-inventory.json",
        "absence_gate_control",
    ),
    (
        "worker",
        "contracts/agent-first/spec-decision-register.json",
        "immutable_decision_history",
    ),
    (
        "worker",
        "docs/agent-first-worker-spec-decision-register.md",
        "immutable_decision_history",
    ),
}
ALLOWED_BOUNDED_EXCLUSIONS = {
    (
        "worker", "AGENTS.md", "d27_evidence",
        "## Agent-First Clean-Break Refactor Policy",
        "## Module And File Size Discipline",
    ),
    (
        "worker", "AGENTS.md", "d27_evidence",
        "## Agent-First Specification Decision Gate",
        "## Agent Kernel Slice 1 Storage Contracts",
    ),
    (
        "worker", "AGENTS.md", "immutable_decision_history",
        "## Agent-First Legacy Policy History",
        "## Worker Host Platform",
    ),
    (
        "worker", "AGENTS.md", "d27_evidence",
        "## Strict V1 Current-State Removal Baseline",
        "## Agent-First Slice 0 Evidence",
    ),
    (
        "worker", "docs/agent-first-worker-design.md", "d27_evidence",
        "## D27 clean-break override\uff08Normative\uff09",
        "## 0. \u7ed3\u8bba\u5148\u884c",
    ),
    (
        "worker", "docs/agent-first-worker-design.md", "d27_evidence",
        "<!-- BEGIN AGENT-FIRST DECISION REFS: TARGET_AUTHORITY_SCOPE -->",
        "<!-- END AGENT-FIRST DECISION REFS: TARGET_AUTHORITY_SCOPE -->",
    ),
    (
        "worker", "docs/agent-first-worker-mvp-implementation-design.md",
        "d27_evidence", "## D27 clean-break override\uff08Normative\uff09",
        "## \u5f53\u524d\u5b9e\u65bd\u72b6\u6001\uff08\u975e\u89c4\u8303\u8bc1\u636e\uff09",
    ),
    (
        "worker", "docs/agent-first-worker-mvp-implementation-design.md",
        "d27_evidence",
        "<!-- BEGIN AGENT-FIRST DECISION REFS: MVP_AUTHORITY_SCOPE -->",
        "<!-- END AGENT-FIRST DECISION REFS: MVP_EXECUTABLE_GATES -->",
    ),
    (
        "worker", "docs/agent-first-worker-post-mvp-implementation-design.md",
        "d27_evidence", "## D27 clean-break override\uff08Normative\uff09",
        "## 0. \u76ee\u7684\u4e0e\u201c\u5b8c\u5168\u5b9e\u73b0\u201d\u7684\u5b9a\u4e49",
    ),
    (
        "worker", "docs/agent-first-worker-post-mvp-implementation-design.md",
        "d27_evidence",
        "<!-- BEGIN AGENT-FIRST DECISION REFS: POST_AUTHORITY_SCOPE -->",
        "<!-- END AGENT-FIRST DECISION REFS: POST_CLOSURE -->",
    ),
}
