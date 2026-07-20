from __future__ import annotations

import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / 'MANIFEST.in'
SETUP_PATH = REPO_ROOT / 'setup.py'
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
        web = next(
            item for item in baseline["repositories"] if item["id"] == "web"
        )
        workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("working-directory: pullwise-worker", workflow)
        self.assertIn("repository: GoPullwise/pullwise-server", workflow)
        self.assertIn(f"ref: {server['frozen_head']}", workflow)
        self.assertIn("path: pullwise-server", workflow)
        self.assertIn("python -m pip install -e ../pullwise-server", workflow)
        self.assertIn("repository: GoPullwise/pullwise-web", workflow)
        self.assertIn(f"ref: {web['frozen_head']}", workflow)
        self.assertIn("path: pullwise-web", workflow)

    def test_packaging_manifests_include_agent_kernel_contract_data(self) -> None:
        manifest_lines = MANIFEST_PATH.read_text(encoding='utf-8').splitlines()

        self.assertIn(
            'recursive-include contracts/agent-task/v1 *.json',
            manifest_lines,
        )
        with patch('setuptools.setup') as setup:
            runpy.run_path(str(SETUP_PATH), run_name='__main__')
        configured = dict(setup.call_args.kwargs['data_files'])
        contract_root = REPO_ROOT / 'contracts' / 'agent-task' / 'v1'

        self.assertEqual(
            {
                path.relative_to(REPO_ROOT).as_posix()
                for path in contract_root.glob('*.json')
            },
            set(configured['share/pullwise-worker/contracts/agent-task/v1']),
        )
        self.assertEqual(
            {
                path.relative_to(REPO_ROOT).as_posix()
                for path in (contract_root / 'fixtures').glob('*.json')
            },
            set(
                configured[
                    'share/pullwise-worker/contracts/agent-task/v1/fixtures'
                ]
            ),
        )

    def test_ci_runs_the_isolated_agent_kernel_wheel_check(self) -> None:
        workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        wheel_check = WHEEL_CHECK_PATH.read_text(encoding="utf-8")

        self.assertTrue(WHEEL_CHECK_PATH.is_file())
        self.assertIn("python scripts/check_agent_kernel_wheel.py", workflow)
        self.assertIn("TaskStore", wheel_check)
        self.assertIn("TaskEventKind.ATTEMPT_CLAIMED", wheel_check)

    def test_ci_runs_default_legacy_absence_ratchet(self) -> None:
        workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        commands = {line.strip() for line in workflow.splitlines()}

        self.assertIn(
            "run: python scripts/verify_agent_first_legacy_absence.py --workspace-root ..",
            commands,
        )
        self.assertNotIn("--require-absent", workflow)


if __name__ == "__main__":
    unittest.main()
