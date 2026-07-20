from __future__ import annotations

import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / 'MANIFEST.in'
BASELINE_PATH = (
    REPO_ROOT / "contracts" / "agent-first" / "legacy-v1-contract-baseline.json"
)
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
WHEEL_CHECK_PATH = REPO_ROOT / "scripts" / "check_agent_kernel_wheel.py"


class CrossRepositoryCiContractTest(unittest.TestCase):
    def test_ci_checks_out_the_frozen_server_as_a_sibling(self) -> None:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        server = next(
            item for item in baseline["repositories"] if item["id"] == "server"
        )
        workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("working-directory: pullwise-worker", workflow)
        self.assertIn("repository: GoPullwise/pullwise-server", workflow)
        self.assertIn(f"ref: {server['frozen_head']}", workflow)
        self.assertIn("path: pullwise-server", workflow)
        self.assertIn("python -m pip install -e ../pullwise-server", workflow)

    def test_source_manifest_includes_agent_kernel_contract_data(self) -> None:
        manifest_lines = MANIFEST_PATH.read_text(encoding='utf-8').splitlines()

        self.assertIn(
            'recursive-include contracts/agent-task/v1 *.json',
            manifest_lines,
        )

    def test_ci_runs_the_isolated_agent_kernel_wheel_check(self) -> None:
        workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        wheel_check = WHEEL_CHECK_PATH.read_text(encoding="utf-8")

        self.assertTrue(WHEEL_CHECK_PATH.is_file())
        self.assertIn("python scripts/check_agent_kernel_wheel.py", workflow)
        self.assertIn("TaskStore", wheel_check)
        self.assertIn("TaskEventKind.ATTEMPT_CLAIMED", wheel_check)


if __name__ == "__main__":
    unittest.main()
