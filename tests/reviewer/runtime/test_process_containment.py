from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "pullwise_worker/reviewer/qualification.py"


def load_qualification():
    if not MODULE_PATH.is_file():
        raise AssertionError("R3Q-02 qualification module is absent")
    spec = importlib.util.spec_from_file_location("reviewer_qualification_process", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ProcessContainmentQualificationTest(unittest.TestCase):
    def test_child_environment_is_rebuilt_from_closed_allowlist(self) -> None:
        qualification = load_qualification()
        fixture = json.loads((Path(__file__).parent / "fixtures/parent-env-secret.json").read_text(encoding="utf-8"))
        source = dict(os.environ)
        source[fixture["name"]] = fixture["value"]
        source["LC_CTYPE"] = "C.UTF-8"
        env = qualification.build_runtime_env(source)
        self.assertEqual(set(qualification.RUNTIME_ENV_KEYS), set(env))
        self.assertNotIn(fixture["name"], env)
        self.assertNotIn("LC_CTYPE", env)

    def test_shell_and_package_commands_are_not_in_catalog(self) -> None:
        qualification = load_qualification()
        for command in ("sh", "bash", "pip", "apt", "git"):
            with self.subTest(command=command), self.assertRaises(qualification.QualificationError):
                qualification.require_cataloged_command(command)


if __name__ == "__main__":
    unittest.main()
