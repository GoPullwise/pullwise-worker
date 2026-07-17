from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.agent_first_decision_gate import (
    git_history_failures,
    historical_resolution_failures,
    normative_marker_failures,
)
from scripts.agent_first_decision_register import (
    canonical_resolution_sha256,
    load_register,
    validate_register,
)
from scripts.agent_first_decision_render import render_normative_marker


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"
NORMATIVE_PATHS = (
    "docs/agent-first-worker-design.md",
    "docs/agent-first-worker-mvp-implementation-design.md",
    "docs/agent-first-worker-post-mvp-implementation-design.md",
)


def _resolution(
    decision_id: str,
    selected_option_id: str,
    *,
    supersedes: str | None = None,
    text: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "option",
        "selected_option_id": selected_option_id,
        "custom_text": None,
        "decision_text": text or f"Confirmed {selected_option_id}.",
        "authority": "architecture_owner",
        "decided_at": "2026-07-17",
        "evidence_refs": ["conversation:synthetic-test"],
        "supersedes_resolution_sha256": supersedes,
    }
    payload["resolution_sha256"] = canonical_resolution_sha256(decision_id, payload)
    return payload


def _resolved_d1(register: dict[str, object]) -> dict[str, object]:
    changed = copy.deepcopy(register)
    decision = changed["decisions"][0]
    decision["status"] = "resolved"
    decision["resolution"] = _resolution("D1", "pullwise_full_scan")
    changed["active_decision_id"] = "D3"
    return changed


def _with_unit(register: dict[str, object]) -> dict[str, object]:
    changed = copy.deepcopy(register)
    changed["normative_units"] = [
        {
            "id": "mvp.authority-scope",
            "path": "docs/agent-first-worker-mvp-implementation-design.md",
            "decision_ids": ["D1"],
        }
    ]
    changed["decisions"][0]["normative_unit_ids"] = ["mvp.authority-scope"]
    validate_register(changed)
    return changed


def _write_docs(root: Path, marker: str | None) -> None:
    for relative in NORMATIVE_PATHS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        selected = relative.endswith("mvp-implementation-design.md")
        path.write_text(
            (marker if selected and marker else "No marker.") + "\n",
            encoding="utf-8",
            newline="\n",
        )


class AgentFirstDecisionRegisterGateTest(unittest.TestCase):
    def test_normative_registration_must_be_bidirectional(self) -> None:
        register = _with_unit(_resolved_d1(load_register(REGISTER_PATH)))
        register["decisions"][0]["normative_unit_ids"] = []
        with self.assertRaisesRegex(Exception, "normative_units:bidirectional"):
            validate_register(register)

    def test_current_resolution_marker_passes_required_slice_gate(self) -> None:
        register = _with_unit(_resolved_d1(load_register(REGISTER_PATH)))
        marker = render_normative_marker(register, register["normative_units"][0])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_docs(root, marker)
            failures = normative_marker_failures(register, root, "S2")
        self.assertEqual([], failures)

    def test_pending_unknown_and_stale_markers_fail_closed(self) -> None:
        pending = _with_unit(load_register(REGISTER_PATH))
        pending_marker = render_normative_marker(pending, pending["normative_units"][0])
        resolved = _with_unit(_resolved_d1(load_register(REGISTER_PATH)))
        current_marker = render_normative_marker(
            resolved, resolved["normative_units"][0]
        )
        digest = resolved["decisions"][0]["resolution"]["resolution_sha256"]
        cases = (
            (pending, pending_marker, "pending_decision_marker"),
            (resolved, current_marker.replace(digest, "0" * 64), "stale_decision_marker"),
            (resolved, current_marker.replace("D1@", "D99@"), "unknown_decision_marker"),
        )
        for register, marker, expected in cases:
            with self.subTest(code=expected), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_docs(root, marker)
                codes = {
                    item["code"]
                    for item in normative_marker_failures(register, root, "S2")
                }
                self.assertIn(expected, codes)

    def test_resolved_required_decision_without_unit_fails(self) -> None:
        register = _resolved_d1(load_register(REGISTER_PATH))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_docs(root, None)
            failures = normative_marker_failures(register, root, "S2")
        self.assertIn(
            {"code": "resolved_decision_missing_normative_unit", "decision_id": "D1"},
            failures,
        )

    def test_historical_resolution_requires_explicit_supersession(self) -> None:
        base = load_register(REGISTER_PATH)
        prior = _resolved_d1(base)
        changed = _resolved_d1(base)
        changed["decisions"][0]["resolution"] = _resolution(
            "D1", "generic_agent_worker", text="Changed in place."
        )
        changed["active_decision_id"] = "D2"
        failures = historical_resolution_failures(changed, [prior])
        prior_digest = prior["decisions"][0]["resolution"]["resolution_sha256"]
        self.assertIn(
            {
                "code": "historical_resolution_missing",
                "decision_id": "D1",
                "resolution_sha256": prior_digest,
            },
            failures,
        )

    def test_explicit_supersession_preserves_prior_resolution(self) -> None:
        prior = _resolved_d1(load_register(REGISTER_PATH))
        prior_resolution = prior["decisions"][0]["resolution"]
        changed = copy.deepcopy(prior)
        changed["decisions"][0]["superseded_resolutions"] = [prior_resolution]
        changed["decisions"][0]["resolution"] = _resolution(
            "D1",
            "generic_agent_worker",
            supersedes=prior_resolution["resolution_sha256"],
            text="Explicitly superseded after owner review.",
        )
        changed["active_decision_id"] = "D2"
        validate_register(changed)
        self.assertEqual([], historical_resolution_failures(changed, [prior]))

    def test_git_history_scan_detects_in_place_rewrite(self) -> None:
        base = load_register(REGISTER_PATH)
        prior = _resolved_d1(base)
        current = _resolved_d1(base)
        current["decisions"][0]["resolution"] = _resolution(
            "D1", "generic_agent_worker", text="Rewritten."
        )
        current["active_decision_id"] = "D2"
        relative = "contracts/agent-first/spec-decision-register.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(root), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            git("init", "-q")
            git("config", "user.email", "decision-gate@example.invalid")
            git("config", "user.name", "Decision Gate Test")
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(prior), encoding="utf-8", newline="\n")
            git("add", ".")
            git("commit", "-qm", "resolved decision")
            path.write_text(json.dumps(current), encoding="utf-8", newline="\n")
            failures = git_history_failures(current, root, relative)
        self.assertIn(
            "historical_resolution_missing", {item["code"] for item in failures}
        )


if __name__ == "__main__":
    unittest.main()
