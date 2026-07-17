from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.agent_first_decision_gate import verify_register
from tests.test_agent_first_decision_register_gate import (
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


if __name__ == "__main__":
    unittest.main()
