"""Fixed catalogs for Agent-First specification decisions and controlled units."""

from __future__ import annotations


SCHEMA_ID = "pullwise-agent-first-spec-decision-register/v1"
REPORT_SCHEMA_ID = "pullwise-agent-first-spec-decision-register-report/v1"
REQUIRED_DEFINITION_SHA256 = (
    "0a88b10e921e4d7b800c65e7a46c0d28eeb129bae58cb3276d9bba594aaa43d3"
)
RESOLUTION_DOMAIN = b"pullwise-agent-first-decision-resolution/v1\0"
SLICES = ("S2", "S3", "S4", "S5", "S6", "S7", "S8")
AUTHORITIES = frozenset({"user", "architecture_owner", "operator"})
ALLOWED_EFFECTS = frozenset(
    {
        "authority",
        "compatibility",
        "data_model",
        "external_behavior",
        "permission",
        "release_ownership",
        "state_semantics",
    }
)
QUESTION_ORDER = (
    "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10",
    "D11", "D12", "D13", "D14", "D15", "D16", "D17", "D18", "D19",
    "D20", "D21", "D23", "D24", "D25", "D26", "D22",
)


def _item(
    decision_id: str,
    key: str,
    scope: str,
    required_by_slice: str,
    dependencies: tuple[str, ...] = (),
    *,
    activation: tuple[str, str] | None = None,
) -> dict[str, object]:
    return {
        "id": decision_id,
        "key": key,
        "scope": scope,
        "required_by_slice": required_by_slice,
        "depends_on": dependencies,
        "activation": activation,
        "source_ref": f"handoff:{scope}",
    }


REQUIRED_CATALOG = (
    _item("D1", "product-scope", "P0.1", "S2"),
    _item(
        "D2", "generic-surface", "P0.1", "S2", ("D1",),
        activation=("D1", "generic_agent_worker"),
    ),
    _item("D3", "mvp-r2", "P0.5", "S3", ("D1",)),
    _item("D4", "policy-source", "P0.4", "S3", ("D1", "D3")),
    _item("D5", "version-unit", "P0.6", "S4", ("D4",)),
    _item("D6", "claim-owner-transaction", "P0.6", "S4", ("D5",)),
    _item("D7", "clock-persistence", "P0.6", "S4"),
    _item("D8", "lease-resume-boundary", "P0.6/P0.7", "S4", ("D5",)),
    _item("D9", "terminal-authority", "P0.7", "S4", ("D8",)),
    _item("D10", "terminal-precedence", "P0.7", "S4", ("D9",)),
    _item("D11", "partial-evidence", "P0.7", "S3", ("D10",)),
    _item("D12", "preack-repair", "P0.7", "S4", ("D9",)),
    _item("D13", "cancel-reconciliation", "P0.7", "S4", ("D9", "D10", "D12")),
    _item("D14", "bundle-ownership", "P0.8", "S4", ("D1",)),
    _item("D15", "gate-code-taxonomy", "P0.3", "S3"),
    _item("D16", "q0-self-attestation", "P0.9", "S3", ("D1", "D4")),
    _item("D17", "q2-slot-plan", "P0.9", "S3", ("D16",)),
    _item("D18", "pipeline-owner", "P0.10", "S5", ("D1", "D6")),
    _item("D19", "owner-liveness", "P0.10", "S5", ("D4", "D18")),
    _item("D20", "legacy-qa-gate", "P0.10", "S5", ("D10", "D17", "D18")),
    _item("D21", "run-mode-owner", "P0.11", "S6", ("D9", "D20")),
    _item("D22", "release-gates", "P0.11", "S6", ("D1", "D20", "D21")),
    _item("D23", "contract-package-owner", "P1.2", "S7", ("D1", "D2")),
    _item("D24", "task-bootstrap", "P1.2", "S7", ("D8", "D23")),
    _item("D25", "receipt-dag", "P1.5", "S7", ("D9", "D23")),
    _item("D26", "post-spec-depth", "P1.6", "S7", ("D1",)),
)
CATALOG_BY_ID = {item["id"]: item for item in REQUIRED_CATALOG}


def _unit(unit_id: str, path: str, required_by_slice: str) -> dict[str, str]:
    slug = unit_id.upper().replace("-", "_")
    return {
        "id": unit_id,
        "path": path,
        "required_by_slice": required_by_slice,
        "start_marker": f"<!-- BEGIN AGENT-FIRST DECISION REFS: {slug} -->",
        "end_marker": f"<!-- END AGENT-FIRST DECISION REFS: {slug} -->",
    }


NORMATIVE_UNIT_CATALOG = (
    _unit("target-authority-scope", "docs/agent-first-worker-design.md", "S2"),
    _unit("mvp-authority-scope", "docs/agent-first-worker-mvp-implementation-design.md", "S2"),
    _unit("post-authority-scope", "docs/agent-first-worker-post-mvp-implementation-design.md", "S2"),
    _unit("mvp-contract-pack", "docs/agent-first-worker-mvp-implementation-design.md", "S3"),
    _unit("mvp-state-semantics", "docs/agent-first-worker-mvp-implementation-design.md", "S4"),
    _unit("mvp-legacy-mapping", "docs/agent-first-worker-mvp-implementation-design.md", "S5"),
    _unit("mvp-executable-gates", "docs/agent-first-worker-mvp-implementation-design.md", "S6"),
    _unit("post-closure", "docs/agent-first-worker-post-mvp-implementation-design.md", "S7"),
)
UNIT_BY_ID = {item["id"]: item for item in NORMATIVE_UNIT_CATALOG}
NORMATIVE_PATHS = tuple(sorted({item["path"] for item in NORMATIVE_UNIT_CATALOG}))
