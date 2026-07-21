from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from scripts.agent_first_decision_catalog import (
    NORMATIVE_PATHS,
    NORMATIVE_UNIT_CATALOG,
    QUESTION_ORDER,
    REQUIRED_CATALOG,
)
from scripts.agent_first_decision_core import decision_applicability
from scripts.agent_first_decision_gate import (
    normative_reference_failures,
    resolved_history_failures,
    verify_register,
)
from scripts.agent_first_decision_register import (
    DecisionRegisterFormatError,
    canonical_resolution_sha256,
    load_register,
    validate_register,
)
from scripts.agent_first_decision_render import render_document


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = (
    REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"
)
def _resolution(
    decision_id: str,
    selected_option_id: str,
    *,
    decision_text: str | None = None,
    supersedes: tuple[str, ...] = (),
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "option",
        "selected_option_id": selected_option_id,
        "custom_text": None,
        "decision_text": decision_text or f"Confirmed {selected_option_id}.",
        "authority": "architecture_owner",
        "decided_at": "2026-07-17",
        "evidence_refs": ["conversation:synthetic-test"],
    }
    payload["resolution_sha256"] = canonical_resolution_sha256(
        decision_id, payload, supersedes
    )
    return payload


def _resolve(
    register: dict[str, object],
    decision_id: str,
    option_id: str,
    *,
    supersedes: tuple[str, ...] = (),
) -> dict[str, object]:
    changed = copy.deepcopy(register)
    decision = next(
        item for item in changed["decisions"] if item["id"] == decision_id
    )
    decision["status"] = "resolved"
    decision["supersedes"] = list(supersedes)
    decision["resolution"] = _resolution(
        decision_id, option_id, supersedes=supersedes
    )
    return changed


def _resolved_d1(option_id: str = "pullwise_full_scan") -> dict[str, object]:
    changed = _resolve(_all_pending_register(), "D1", option_id)
    changed["active_decision_id"] = (
        "D2" if option_id == "generic_agent_worker" else "D3"
    )
    return validate_register(changed)


def _pending_d1() -> dict[str, object]:
    return validate_register(_all_pending_register())


def _all_pending_register() -> dict[str, object]:
    changed = copy.deepcopy(load_register(REGISTER_PATH))
    required_ids = {item["id"] for item in REQUIRED_CATALOG}
    changed["decisions"] = changed["decisions"][: len(REQUIRED_CATALOG)]
    changed["question_order"] = list(QUESTION_ORDER)
    for unit in changed["normative_units"]:
        unit["decision_ids"] = [item for item in unit["decision_ids"] if item in required_ids]
    for decision in changed["decisions"]:
        decision["status"] = "pending"
        decision["resolution"] = None
    changed["active_decision_id"] = "D1"
    return changed


def _unit_body(
    register: dict[str, object], unit_id: str
) -> str | None:
    decisions = {item["id"]: item for item in register["decisions"]}
    unit = next(
        item for item in register["normative_units"] if item["id"] == unit_id
    )
    tokens: list[str] = []
    for decision_id in unit["decision_ids"]:
        applicability = decision_applicability(register, decision_id)
        if applicability == "inactive":
            continue
        decision = decisions[decision_id]
        if applicability == "active" and decision["status"] == "resolved":
            digest = decision["resolution"]["resolution_sha256"]
            tokens.append(f"<!-- {decision_id}@sha256:{digest} -->")
    return "\n".join(tokens) if tokens else None


def _write_normative_docs(
    root: Path,
    register: dict[str, object],
    *,
    overrides: dict[str, str | None] | None = None,
    include_ready: bool = True,
) -> None:
    overrides = overrides or {}
    for relative in NORMATIVE_PATHS:
        lines = ["# Synthetic normative document", ""]
        for unit in NORMATIVE_UNIT_CATALOG:
            if unit["path"] != relative:
                continue
            body = (
                overrides[unit["id"]]
                if unit["id"] in overrides
                else _unit_body(register, unit["id"])
            )
            if body is None or (
                not include_ready and unit["id"] not in overrides
            ):
                continue
            lines.extend(
                [unit["start_marker"], body, unit["end_marker"], ""]
            )
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _append_decision(
    register: dict[str, object],
    *,
    decision_id: str = "D27",
    question_index: int = 1,
) -> dict[str, object]:
    changed = copy.deepcopy(register)
    template = copy.deepcopy(changed["decisions"][0])
    affected_units = list(changed["decisions"][0]["affected_units"])
    template.update(
        {
            "id": decision_id,
            "key": f"synthetic-{decision_id.lower()}",
            "scope": "test-only",
            "title": f"Synthetic {decision_id}",
            "question": f"Resolve synthetic {decision_id}?",
            "status": "pending",
            "depends_on": [],
            "activation": None,
            "required_by_slice": "S8",
            "effects": ["authority"],
            "source_refs": ["test:synthetic"],
            "affected_units": affected_units,
            "resolution": None,
            "supersedes": [],
        }
    )
    changed["decisions"].append(template)
    for unit in changed["normative_units"]:
        if unit["id"] in affected_units:
            unit["decision_ids"].append(decision_id)
    changed["question_order"].insert(question_index, decision_id)
    return changed


def _append_followup(
    register: dict[str, object],
    *,
    source_id: str = "D27",
    decision_id: str = "D28",
    question_index: int = 2,
) -> dict[str, object]:
    changed = copy.deepcopy(register)
    template = copy.deepcopy(changed["decisions"][1])
    template.update(
        {
            "id": decision_id,
            "key": f"synthetic-{decision_id.lower()}",
            "status": "pending",
            "depends_on": [source_id],
            "activation": {
                "decision_id": source_id,
                "selected_option_id": "generic_agent_worker",
            },
            "source_refs": [
                *template["source_refs"],
                "test:synthetic-followup",
            ],
            "resolution": None,
            "supersedes": [],
        }
    )
    changed["decisions"].append(template)
    for unit in changed["normative_units"]:
        if unit["id"] in template["affected_units"]:
            unit["decision_ids"].append(decision_id)
    changed["question_order"].insert(question_index, decision_id)
    return changed


class AgentFirstDecisionRegisterGateTest(unittest.TestCase):
    def test_bidirectional_registration_rejects_one_sided_change(self) -> None:
        register = load_register(REGISTER_PATH)
        register["normative_units"][0]["decision_ids"].remove("D1")
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "normative_units:bidirectional"
        ):
            validate_register(register)

    def test_resolved_references_are_required_even_without_slice_flag(self) -> None:
        register = _resolved_d1()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_normative_docs(root, register, include_ready=False)
            missing = normative_reference_failures(
                register, root, require_slice=None
            )
            _write_normative_docs(root, register)
            complete = normative_reference_failures(
                register, root, require_slice=None
            )
        missing_units = {
            item["unit_id"]
            for item in missing
            if item["code"] == "normative_unit_reference_missing"
        }
        self.assertEqual(
            {
                "target-authority-scope",
                "mvp-authority-scope",
                "post-authority-scope",
            },
            missing_units,
        )
        self.assertEqual([], complete)

    def test_pending_empty_marker_is_allowed_but_pending_token_is_not(self) -> None:
        register = _pending_d1()
        unit_id = "target-authority-scope"
        pending_token = f"<!-- D1@sha256:{'0' * 64} -->"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_normative_docs(
                root, register, overrides={unit_id: ""}
            )
            empty_failures = normative_reference_failures(
                register, root, require_slice=None
            )
            _write_normative_docs(
                root, register, overrides={unit_id: pending_token}
            )
            pending_failures = normative_reference_failures(
                register, root, require_slice=None
            )
        self.assertEqual([], empty_failures)
        self.assertIn(
            "pending_decision_reference",
            {item["code"] for item in pending_failures},
        )

    def test_unknown_stale_malformed_and_unscoped_references_fail(self) -> None:
        register = _resolved_d1()
        decision = register["decisions"][0]
        digest = decision["resolution"]["resolution_sha256"]
        token = f"D1@sha256:{digest}"
        cases = {
            "unknown_decision_reference": token.replace("D1@", "D99@"),
            "stale_decision_reference": token.replace(digest, "0" * 64),
            "malformed_decision_reference": "D1@sha256:ABC",
        }
        for expected, replacement in cases.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                body = f"<!-- {replacement} -->"
                _write_normative_docs(
                    root,
                    register,
                    overrides={"target-authority-scope": body},
                )
                codes = {
                    item["code"]
                    for item in normative_reference_failures(
                        register, root, require_slice=None
                    )
                }
                self.assertIn(expected, codes)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_normative_docs(root, register)
            path = root / NORMATIVE_PATHS[0]
            path.write_text(
                path.read_text(encoding="utf-8")
                + f"\n<!-- {token} -->\n",
                encoding="utf-8",
                newline="\n",
            )
            codes = {
                item["code"]
                for item in normative_reference_failures(
                    register, root, require_slice=None
                )
            }
        self.assertIn("unscoped_decision_reference", codes)

    def test_slice_gate_reports_every_due_active_pending_decision(self) -> None:
        register = load_register(REGISTER_PATH)
        report = verify_register(
            register,
            REPO_ROOT,
            require_slice="S5",
            check_document=False,
            check_history=False,
        )
        blocker = next(
            item
            for item in report["failures"]
            if item["code"] == "slice_blocked_by_pending_decisions"
        )
        self.assertEqual(
            ["D20"],
            blocker["decision_ids"],
        )
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])

        report = verify_register(
            register, REPO_ROOT, require_slice="S7",
            check_document=False, check_history=False,
        )
        blocker = next(item for item in report["failures"]
                       if item["code"] == "slice_blocked_by_pending_decisions")
        self.assertEqual(["D20", "D21", *[f"D{i}" for i in range(23, 27)], "D22"],
                         blocker["decision_ids"])

    def test_rendered_resolution_contains_text_evidence_digest_and_relation(self) -> None:
        register = _resolved_d1()
        rendered = render_document(register)
        resolution = register["decisions"][0]["resolution"]
        self.assertIn(
            "- `pullwise_full_scan` — selected by resolution:", rendered
        )
        self.assertNotIn(
            "`pullwise_full_scan` — non-normative recommendation, not selected",
            rendered,
        )
        self.assertIn(resolution["decision_text"], rendered)
        self.assertIn("conversation:synthetic-test", rendered)
        self.assertIn(resolution["resolution_sha256"], rendered)
        self.assertIn("**Supersedes:** none", rendered)

    def test_resolved_history_is_immutable(self) -> None:
        prior = _resolved_d1()
        self.assertEqual([], resolved_history_failures(prior, [prior]))

        changed = _resolved_d1("generic_agent_worker")
        codes = {
            item["code"]
            for item in resolved_history_failures(changed, [prior])
        }
        self.assertIn("resolved_decision_not_immutable", codes)

        removed = copy.deepcopy(prior)
        removed["decisions"] = removed["decisions"][1:]
        codes = {
            item["code"]
            for item in resolved_history_failures(removed, [prior])
        }
        self.assertIn("resolved_decision_not_immutable", codes)

    def test_resolved_decisions_cannot_skip_the_question_order(self) -> None:
        register = load_register(REGISTER_PATH)
        option_id = register["decisions"][20]["options"][0]["id"]
        changed = _resolve(register, "D21", option_id)
        with self.assertRaisesRegex(
            DecisionRegisterFormatError, "out_of_question_order"
        ):
            validate_register(changed)

if __name__ == "__main__":
    unittest.main()
