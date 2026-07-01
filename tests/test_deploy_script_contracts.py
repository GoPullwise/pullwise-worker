from __future__ import annotations

from pathlib import Path
import re
import unittest


class DeployScriptContractsTest(unittest.TestCase):
    def deploy_scripts(self) -> list[Path]:
        root = Path(__file__).resolve().parents[1]
        return sorted((root / "deploy").glob("*-worker.sh")) + [root / "deploy" / "cleanup-checkouts.sh"]

    def test_deploy_maintenance_scripts_do_not_add_live_nodesource_repositories(self) -> None:
        for path in self.deploy_scripts():
            script = path.read_text(encoding="utf-8")
            with self.subTest(script=path.name):
                self.assertNotIn("deb.nodesource.com", script)
                self.assertNotIn("nodesource", script.lower())
                self.assertNotIn("needs_nodesource", script)
                self.assertNotIn("install_nodesource_nodejs", script)

    def test_deploy_maintenance_scripts_do_not_pipe_remote_content_to_shell(self) -> None:
        remote_shell_pipe = re.compile(r"curl\b[^\n|]*\|\s*(?:sh|bash)\b")
        for path in self.deploy_scripts():
            script = path.read_text(encoding="utf-8")
            with self.subTest(script=path.name):
                self.assertIsNone(remote_shell_pipe.search(script))


if __name__ == "__main__":
    unittest.main()