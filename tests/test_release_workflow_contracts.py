from __future__ import annotations

from pathlib import Path
import unittest


class ReleaseWorkflowContractsTest(unittest.TestCase):
    def release_workflow(self) -> str:
        return (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    def test_release_workflow_restricts_publishable_refs(self) -> None:
        workflow = self.release_workflow()

        self.assertIn("Manual releases must run from refs/heads/main", workflow)
        self.assertIn('github.ref }}" != "refs/heads/main"', workflow)
        self.assertIn('git merge-base --is-ancestor "$GITHUB_SHA" origin/main', workflow)
        self.assertIn("Release tags must point to commits reachable from origin/main.", workflow)

    def test_release_workflow_runs_ci_gates_before_publishing(self) -> None:
        workflow = self.release_workflow()
        publish_index = workflow.index("Create or update GitHub Release")
        expected_gates = [
            "pip-audit .",
            "python -m pip check",
            "bash -n ./deploy/update-worker.sh",
            "python -m unittest discover -s tests -p",
        ]

        for gate in expected_gates:
            with self.subTest(gate=gate):
                self.assertIn(gate, workflow)
                self.assertLess(workflow.index(gate), publish_index)


if __name__ == "__main__":
    unittest.main()