from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "pullwise_worker/reviewer/qualification.py"


def load_qualification():
    if not MODULE_PATH.is_file():
        raise AssertionError("R3Q-02 qualification module is absent")
    spec = importlib.util.spec_from_file_location("reviewer_qualification_fs", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class FilesystemNetworkEnvironmentQualificationTest(unittest.TestCase):
    def test_write_boundary_denies_source_and_escape(self) -> None:
        qualification = load_qualification()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            scratch = root / "scratch"
            source.mkdir()
            scratch.mkdir()
            qualification.require_allowed_write(scratch / "ok.json", [scratch])
            for denied in (source / "change.py", root.parent / "escape"):
                with self.subTest(denied=denied), self.assertRaises(qualification.QualificationError):
                    qualification.require_allowed_write(denied, [scratch])

    def test_proxy_policy_is_explicit_credential_free_and_non_distributable(self) -> None:
        qualification = load_qualification()
        policy = qualification.environment_policy()
        self.assertFalse(policy["inherit_ambient"])
        self.assertEqual("explicit_external_proxy", policy["variables"]["HTTPS_PROXY"])
        self.assertNotIn("10.12.28.126", qualification.canonical_bytes(policy).decode("utf-8"))
        qualification.validate_proxy_url("http://10.12.28.126:7890")
        with self.assertRaises(qualification.QualificationError):
            qualification.validate_proxy_url("http://user:password@10.12.28.126:7890")


if __name__ == "__main__":
    unittest.main()
