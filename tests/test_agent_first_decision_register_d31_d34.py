from __future__ import annotations

import unittest
from pathlib import Path

from scripts.agent_first_decision_register import load_register


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"

EXPECTED_DECISIONS = {
    "D31": {
        "key": "server-owned-deadline-wire",
        "depends_on": ["D7", "D21", "D23", "D29", "D30"],
        "required_by_slice": "S4",
        "recommended_option_id": "server_owned_immutable_deadline_wire",
        "option_ids": [
            "server_owned_immutable_deadline_wire",
            "worker_derived_deadline_wire",
        ],
        "evidence_ref": (
            "conversation:user-approval:2026-07-23:"
            "D7:server_owned_immutable_deadline_wire"
        ),
    },
    "D32": {
        "key": "transport-abandonment-record",
        "depends_on": ["D5", "D8", "D23", "D25", "D29"],
        "required_by_slice": "S4",
        "recommended_option_id": "independent_transport_abandonment_record",
        "option_ids": [
            "independent_transport_abandonment_record",
            "abandon_response_is_evidence_record",
        ],
        "evidence_ref": (
            "conversation:user-approval:2026-07-23:"
            "D8:independent_transport_abandonment_record"
        ),
    },
    "D33": {
        "key": "canonical-terminal-selector",
        "depends_on": ["D9", "D10", "D11", "D13", "D20", "D23", "D29"],
        "required_by_slice": "S4",
        "recommended_option_id": "canonical_mechanical_terminal_selector",
        "option_ids": [
            "canonical_mechanical_terminal_selector",
            "caller_selected_terminal_outcome",
        ],
        "evidence_ref": (
            "conversation:user-approval:2026-07-23:"
            "D10:canonical_mechanical_terminal_selector"
        ),
    },
    "D34": {
        "key": "current-candidate-activation-boundary",
        "depends_on": [
            "D22", "D23", "D24", "D27", "D28", "D29", "D31", "D32", "D33"
        ],
        "required_by_slice": "S7",
        "recommended_option_id": "candidate_only_no_activation",
        "option_ids": [
            "candidate_only_no_activation",
            "activate_d24_and_production_routes",
        ],
        "evidence_ref": (
            "conversation:user-approval:2026-07-23:"
            "candidate_only_no_activation"
        ),
    },
}


class AgentFirstDecisionRegisterD31D34Test(unittest.TestCase):
    def test_approved_follow_up_decisions_are_exact_append_only_suffix(self) -> None:
        register = load_register(REGISTER_PATH)
        decisions = {item["id"]: item for item in register["decisions"]}

        self.assertEqual(["D31", "D32", "D33", "D34"], register["question_order"][-4:])
        self.assertEqual(["D31", "D32", "D33", "D34"], [
            item["id"] for item in register["decisions"][-4:]
        ])
        for decision_id, expected in EXPECTED_DECISIONS.items():
            with self.subTest(decision_id=decision_id):
                decision = decisions[decision_id]
                self.assertEqual("resolved", decision["status"])
                self.assertEqual(expected["key"], decision["key"])
                self.assertEqual(expected["depends_on"], decision["depends_on"])
                self.assertEqual(
                    expected["required_by_slice"], decision["required_by_slice"]
                )
                self.assertEqual([], decision["supersedes"])
                self.assertEqual(
                    expected["recommended_option_id"],
                    decision["recommended_option_id"],
                )
                self.assertEqual(
                    expected["option_ids"],
                    [option["id"] for option in decision["options"]],
                )
                resolution = decision["resolution"]
                self.assertEqual("option", resolution["kind"])
                self.assertEqual(
                    expected["recommended_option_id"],
                    resolution["selected_option_id"],
                )
                self.assertIsNone(resolution["custom_text"])
                self.assertEqual("user", resolution["authority"])
                self.assertEqual("2026-07-23", resolution["decided_at"])
                self.assertEqual(
                    [expected["evidence_ref"]], resolution["evidence_refs"]
                )

    def test_d31_freezes_server_owned_immutable_deadline_wire(self) -> None:
        decision = self._decision("D31")
        text = decision["resolution"]["decision_text"]

        for invariant in (
            "accepted_at + effective_policy.budgets.wall_ms",
            "absolute_deadline_at",
            "terminalization_reserve_ms",
            "agent-worker-grant/v1",
            "server-authority-envelope/v1",
            "never recomputes",
        ):
            self.assertIn(invariant, text)

    def test_d32_keeps_abandonment_evidence_distinct_from_response_authority(self) -> None:
        decision = self._decision("D32")
        text = decision["resolution"]["decision_text"]

        for invariant in (
            "transport-abandonment-record/v1",
            "agent-claim-abandon-response/v1",
            "distinct canonical bytes and digests",
            "must not terminalize",
            "must not bind a transport receipt",
        ):
            self.assertIn(invariant, text)

    def test_d33_freezes_six_axis_mechanical_selector(self) -> None:
        decision = self._decision("D33")
        text = decision["resolution"]["decision_text"]

        for axis in (
            "profile",
            "gate_mode",
            "cancel_state",
            "effect_state",
            "cause_family",
            "delivery_state",
        ):
            self.assertIn(axis, text)
        for invariant in (
            "CANCELLED_WITH_EFFECTS",
            "TERMINATED_WITH_UNKNOWN_EFFECTS",
            "RECONCILING",
            "caller-supplied outcome",
            "tombstone/delete fence",
        ):
            self.assertIn(invariant, text)

    def test_d34_keeps_candidate_and_future_route_auth_work_unactivated(self) -> None:
        decision = self._decision("D34")
        text = decision["resolution"]["decision_text"]

        for invariant in (
            "candidate package",
            "current-task/operator route",
            "auth boundary",
            "D24 barrier",
            "production Worker loop",
            "deployment",
            "canary",
        ):
            self.assertIn(invariant, text)

    def _decision(self, decision_id: str) -> dict[str, object]:
        register = load_register(REGISTER_PATH)
        return next(
            item for item in register["decisions"] if item["id"] == decision_id
        )


if __name__ == "__main__":
    unittest.main()
