from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.agent_first_decision_gate import verify_register
from scripts.agent_first_decision_register import validate_register
from tests.test_agent_first_decision_register_gate import (
    _append_decision,
    _append_followup,
    _resolve,
    _resolved_d1,
    _write_normative_docs,
)


MANIFEST_RELATIVE = "contracts/agent-first/spec-decision-register.json"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _write_manifest(root: Path, register: dict[str, object]) -> Path:
    path = root / MANIFEST_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(register, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


class AgentFirstDecisionRegisterHistoryTest(unittest.TestCase):
    def test_supersession_after_prior_answers_preserves_question_order(self) -> None:
        progressed = _resolved_d1()
        d3_option = progressed["decisions"][2]["options"][0]["id"]
        progressed = _resolve(progressed, "D3", d3_option)
        progressed["active_decision_id"] = "D4"

        pending = _append_decision(progressed, question_index=3)
        pending = _append_followup(pending, question_index=4)
        pending["active_decision_id"] = "D27"
        successor = _resolve(
            pending,
            "D27",
            "generic_agent_worker",
            supersedes=("D1",),
        )
        successor["active_decision_id"] = "D28"
        followup = next(
            item for item in successor["decisions"] if item["id"] == "D28"
        )
        closed = _resolve(
            successor, "D28", followup["options"][0]["id"]
        )
        closed["active_decision_id"] = "D4"

        validate_register(progressed)
        validate_register(pending)
        validate_register(successor)
        validate_register(closed)

    def test_git_history_detects_rewrite_deletion_and_unknown_schema(self) -> None:
        prior = _resolved_d1()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _git(root, "init", "-q")
            _git(root, "config", "user.email", "gate@example.invalid")
            _git(root, "config", "user.name", "Decision Gate")
            _write_manifest(root, prior)
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "resolved")
            _write_normative_docs(root, prior)

            rewritten = _resolved_d1("generic_agent_worker")
            _write_manifest(root, rewritten)
            rewrite_report = verify_register(
                rewritten, root, check_document=False
            )
            self.assertIn(
                "resolved_decision_not_immutable",
                {item["code"] for item in rewrite_report["failures"]},
            )

            _write_manifest(root, prior)
            _git(root, "rm", "-q", MANIFEST_RELATIVE)
            _git(root, "commit", "-qm", "delete register")
            _write_manifest(root, prior)
            delete_report = verify_register(
                prior, root, check_document=False
            )
            self.assertIn(
                "historical_manifest_deleted",
                {item["code"] for item in delete_report["failures"]},
            )

            unsupported = copy.deepcopy(prior)
            unsupported["schema_id"] = "unknown/v2"
            _write_manifest(root, unsupported)
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "unknown schema")
            _write_manifest(root, prior)
            schema_report = verify_register(
                prior, root, check_document=False
            )
            self.assertIn(
                "historical_schema_unsupported",
                {item["code"] for item in schema_report["failures"]},
            )

            path = _write_manifest(root, prior)
            path.write_text("[]\n", encoding="utf-8", newline="\n")
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "non-object manifest")
            _write_manifest(root, prior)
            non_object_report = verify_register(
                prior, root, check_document=False
            )
            self.assertIn(
                "historical_manifest_invalid",
                {item["code"] for item in non_object_report["failures"]},
            )


if __name__ == "__main__":
    unittest.main()
