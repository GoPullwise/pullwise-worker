from __future__ import annotations

from pathlib import Path
import unittest

from pullwise_worker import _generated_agent_task_contract as generated_contract
from pullwise_worker.agent_kernel_current_package import CURRENT_PACKAGE


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = REPO_ROOT.parent / "pullwise-server"
WORKER_WRAPPER = REPO_ROOT / "pullwise_worker" / "_generated_agent_task_contract.py"
SERVER_WRAPPER = (
    SERVER_ROOT / "pullwise_server" / "_generated_agent_task_contract.py"
)
SERVER_BUNDLE = (
    SERVER_ROOT
    / "contracts"
    / "agent-first"
    / "current"
    / "published"
    / "contract-bundle.json"
)


class AgentKernelCurrentPackageTest(unittest.TestCase):
    def test_worker_pin_is_the_generated_package_tuple(self) -> None:
        self.assertEqual(generated_contract.PACKAGE_TUPLE, CURRENT_PACKAGE.as_tuple())
        generated_contract.verify_bundle()

    def test_worker_wrapper_is_exact_server_artifact(self) -> None:
        self.assertTrue(SERVER_WRAPPER.is_file(), "Server wrapper artifact is required")
        self.assertTrue(SERVER_BUNDLE.is_file(), "Server bundle artifact is required")
        self.assertEqual(SERVER_WRAPPER.read_bytes(), WORKER_WRAPPER.read_bytes())
        self.assertEqual(SERVER_BUNDLE.read_bytes(), generated_contract.bundle_bytes())


if __name__ == "__main__":
    unittest.main()
