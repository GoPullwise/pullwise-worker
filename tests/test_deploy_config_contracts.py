from __future__ import annotations

import unittest
from pathlib import Path


class DeployConfigContractsTest(unittest.TestCase):
    def test_worker_env_template_omits_server_owned_policy_defaults(self) -> None:
        template = (
            Path(__file__).resolve().parents[1] / "deploy" / "worker.env.template"
        ).read_text(encoding="utf-8")

        self.assertIn("PULLWISE_CODEX_MODEL=gpt-5.5", template)
        self.assertNotIn("PULLWISE_CODEX_REASONING_EFFORT", template)
        self.assertNotIn("PULLWISE_CODEX_TIMEOUT_SECONDS", template)


if __name__ == "__main__":
    unittest.main()
