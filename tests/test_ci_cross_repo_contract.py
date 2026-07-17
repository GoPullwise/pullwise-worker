from __future__ import annotations

import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = (
    REPO_ROOT / "contracts" / "agent-first" / "legacy-v1-contract-baseline.json"
)
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


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


if __name__ == "__main__":
    unittest.main()
