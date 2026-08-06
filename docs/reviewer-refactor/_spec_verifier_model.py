from __future__ import annotations

from pathlib import Path


SPEC_VERSION = "2026-08-06-r4"
SPEC_ID = "pullwise-reviewer-refactor/v1"
SPEC_DIR = Path("docs/reviewer-refactor")
MANIFEST_REL = SPEC_DIR / "spec-manifest.json"
MAIN_REL = Path("docs/codex-sdk-reviewer-skill-worker-refactor-proposal.md")
SAFE_INT_MAX = 9_007_199_254_740_991
COMMAND_FIELDS = (
    "red_commands",
    "green_commands",
    "focused_commands",
    "full_commands",
    "ci_commands",
)
CARD_IDS = (
    "COL-0D",
    "COL-0F",
    "GOV-0A",
    "EVD-0",
    "GOV-0B",
    "EVD-1",
    "CON-0",
    "BEN-0",
    "SKILL-1",
    "RUN-1",
    "RUN-2",
    "RES-1",
    "PUB-1",
    "BEN-1",
    "SRV-1",
    "CON-1",
    "SRV-2",
    "WEB-1",
    "ADM-1",
    "CUT-1",
    "REL-1",
    "CAN-5",
    "CAN-25",
    "PROM-1",
)
GATE_IDS = tuple(
    f"SPEC-READY-{index:02d}-{name}"
    for index, name in enumerate(
        (
            "AUTHORITY",
            "INSTRUCTIONS",
            "MANIFEST",
            "BOOTSTRAP",
            "EVIDENCE",
            "CONTRACT",
            "SECURITY",
            "SKILL",
            "CONTEXT",
            "CAPABILITY",
            "RELEASE",
            "EXECUTION",
        ),
        start=1,
    )
)
REQUIRED_FILES = (
    MAIN_REL.as_posix(),
    "docs/reviewer-refactor/_spec_verifier_cards.py",
    "docs/reviewer-refactor/_spec_verifier_core.py",
    "docs/reviewer-refactor/_spec_verifier_json.py",
    "docs/reviewer-refactor/_spec_verifier_model.py",
    "docs/reviewer-refactor/_spec_verifier_selftest.py",
    "docs/reviewer-refactor/agent-entry.json",
    "docs/reviewer-refactor/authority-and-readiness.md",
    "docs/reviewer-refactor/bootstrap-command.json",
    "docs/reviewer-refactor/evidence-and-determinism.md",
    "docs/reviewer-refactor/execution-card.schema.json",
    "docs/reviewer-refactor/execution-cards.json",
    "docs/reviewer-refactor/fixtures/spec-verifier/invalid/dependency-cycle.json",
    "docs/reviewer-refactor/fixtures/spec-verifier/invalid/float-confidence.json",
    "docs/reviewer-refactor/fixtures/spec-verifier/invalid/path-escape.json",
    "docs/reviewer-refactor/fixtures/spec-verifier/manifest.json",
    "docs/reviewer-refactor/fixtures/spec-verifier/valid/scalar-profile.json",
    "docs/reviewer-refactor/operations-and-execution.md",
    "docs/reviewer-refactor/readiness.json",
    "docs/reviewer-refactor/runtime-contract-and-security.md",
    "docs/reviewer-refactor/skill-context-and-evaluation.md",
    "docs/reviewer-refactor/verify_spec.py",
    "tests/test_reviewer_refactor_spec.py",
)
REQUIRED_CARD_PATHS = {
    "RUN-1": {
        ("worker", "pullwise_worker/reviewer_runtime/__init__.py"),
        ("worker", "pullwise_worker/reviewer_runtime/types.py"),
        ("worker", "pullwise_worker/reviewer_runtime/validation_service.py"),
        ("worker", "tests/test_reviewer_validation_service.py"),
    },
    "RUN-2": {
        ("worker", "scripts/run_reviewer_candidate.py"),
        ("worker", "tests/test_reviewer_model_fs_policy.py"),
        ("worker", "tests/test_reviewer_runtime_policy.py"),
    },
    "WEB-1": {
        ("web", "src/api/pullwise.js"),
        ("web", "src/lib/pullwise-data.js"),
        ("web", "src/screens/flow.jsx"),
        ("web", "src/screens/issues.jsx"),
    },
    "ADM-1": {
        ("admin", "src/api/pullwise.js"),
        ("admin", "src/screens/plans.jsx"),
        ("admin", "src/screens/settings.jsx"),
    },
    "SRV-2": {
        ("server", "pullwise_server/db.py"),
        ("server", "pullwise_server/_app_part_04_scan_audit_bundle.py"),
        ("server", "pullwise_server/_app_part_05_worker_results.py"),
        ("server", "pullwise_server/_app_part_10_handler_main.py"),
    },
    "CUT-1": {
        ("worker", "pullwise_worker/main.py"),
        ("worker", "pullwise_worker/review_worker_v1.py"),
        ("server", "tests/test_review_worker_protocol_v1.py"),
        ("web", "contract-package-pin.json"),
    },
}
TRANSITION_TRANSACTION = (
    "publish-content-addressed-successor",
    "bind-authorized-card-commands",
    "bind-authority-record-digests",
    "advance-agent-entry-cas",
    "verify-successor-self-test",
)
GENERATION_ATOMIC_CHANGES = (
    "execution-cards generation, profile, transition, card states, and commands",
    "agent-entry current_generation, execution_profile, authority_state, and next_card_id",
    "bootstrap-command card_generation and execution_profile binding",
    "readiness evidence and status without weakening failed gates",
    "spec-manifest file sizes and digests",
)
ENTRY_ACTION_COMMANDS = {
    "verify-spec": (
        (
            "worker",
            (
                "python", "-I", "-B",
                "docs/reviewer-refactor/verify_spec.py", "--self-test",
            ),
            (0,),
        ),
    ),
    "inspect-current-gates": (
        (
            "worker",
            (
                "python", "scripts/agent_first_decision_register.py", "check",
                "--repo-root", ".", "--require-slice", "S8",
            ),
            (0, 1, 2),
        ),
        (
            "worker",
            (
                "python", "scripts/verify_agent_first_contract_baseline.py",
                "check", "--workspace-root", "..",
            ),
            (0, 1, 2),
        ),
        (
            "worker",
            (
                "python", "scripts/verify_agent_first_legacy_absence.py",
                "--workspace-root", "..",
            ),
            (0, 1, 2),
        ),
        (
            "worker",
            (
                "python", "scripts/verify_agent_first_legacy_absence.py",
                "--workspace-root", "..", "--require-absent",
            ),
            (0, 1, 2),
        ),
        (
            "worker",
            (
                "python", "scripts/agent_first_slice0_baseline.py", "check",
                "--repo-root", ".",
            ),
            (0, 1, 2),
        ),
    ),
}
LINE_POLICY = {"default_max": 400, "review_max": 600, "hard_max": 600}
