from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
SDK_REQUIREMENT = "openai-codex==0.1.0b3"
LOCK_PATH = REPO_ROOT / "runtime" / "reviewer-runtime-lock.json"
LOCK_DIGEST = "sha256:48e6f0cedbd54f686008b83298fdc81c470293845b2304137535483f481b1399"
ENVIRONMENT_POLICY_DIGEST = (
    "sha256:ee8cb059334e30796df18f08e21b0593db8adf2170d1f2df2fd60619962abcd1"
)
SANDBOX_POLICY_DIGEST = (
    "sha256:eccc19a3115c7b35f267592fc55642ecd307b23c84c869da354f01ddf158e85a"
)
QUALIFIER_PATH = REPO_ROOT / "scripts" / "qualify_reviewer_runtime.py"


def load_qualifier():
    spec = importlib.util.spec_from_file_location("qualify_reviewer_runtime", QUALIFIER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load runtime qualifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pinned_sdk_requirements(path: Path) -> list[str]:
    return re.findall(r'["\'](openai-codex==[^"\']+)["\']', path.read_text(encoding="utf-8"))


class ReviewerRuntimeInstallIdentityTest(unittest.TestCase):
    def test_supported_install_metadata_pins_the_same_exact_sdk(self) -> None:
        self.assertEqual(
            [SDK_REQUIREMENT],
            pinned_sdk_requirements(REPO_ROOT / "pyproject.toml"),
        )
        self.assertEqual(
            [SDK_REQUIREMENT],
            pinned_sdk_requirements(REPO_ROOT / "setup.py"),
        )

    def test_runtime_lock_is_the_closed_candidate_identity(self) -> None:
        lock_bytes = LOCK_PATH.read_bytes()
        lock = json.loads(lock_bytes)
        self.assertEqual(
            {
                "allowlisted_for_reviewer",
                "cli_executable",
                "cli_package",
                "environment_policy_sha256",
                "os",
                "python_version",
                "qualification_report_sha256",
                "sandbox_policy_sha256",
                "schema_id",
                "sdk",
            },
            set(lock),
        )
        self.assertEqual("pullwise-codex-runtime-lock/v1", lock["schema_id"])
        self.assertEqual("ubuntu-22.04-x86_64", lock["os"])
        self.assertEqual("3.10.12", lock["python_version"])
        self.assertEqual(
            {"name", "version", "wheel_sha256"},
            set(lock["sdk"]),
        )
        self.assertEqual("openai-codex", lock["sdk"]["name"])
        self.assertEqual("0.1.0b3", lock["sdk"]["version"])
        self.assertEqual(
            {
                "name",
                "version",
                "artifact_sha256",
            },
            set(lock["cli_package"]),
        )
        self.assertEqual("openai-codex-cli-bin", lock["cli_package"]["name"])
        self.assertEqual("0.137.0a4", lock["cli_package"]["version"])
        self.assertEqual("runtime/bin/codex", lock["cli_executable"]["instance_relative_path"])
        self.assertFalse(Path(lock["cli_executable"]["instance_relative_path"]).is_absolute())
        self.assertEqual(ENVIRONMENT_POLICY_DIGEST, lock["environment_policy_sha256"])
        self.assertEqual(SANDBOX_POLICY_DIGEST, lock["sandbox_policy_sha256"])
        self.assertRegex(lock["qualification_report_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(lock["allowlisted_for_reviewer"])

    def test_qualifier_accepts_only_the_closed_unqualified_lock(self) -> None:
        qualifier = load_qualifier()
        lock = qualifier.load_runtime_lock(LOCK_PATH)
        self.assertEqual(LOCK_DIGEST, qualifier.runtime_identity_sha256(lock))
        self.assertEqual(
            (ENVIRONMENT_POLICY_DIGEST, SANDBOX_POLICY_DIGEST),
            qualifier.policy_digests(),
        )
        self.assertRegex(lock["qualification_report_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(lock["allowlisted_for_reviewer"])

    def test_qualifier_rejects_runtime_lock_drift(self) -> None:
        qualifier = load_qualifier()
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        lock["cli_executable"]["byte_size"] += 1
        with tempfile.TemporaryDirectory() as directory:
            tampered_path = Path(directory) / "runtime-lock.json"
            tampered_path.write_text(json.dumps(lock), encoding="utf-8")
            with self.assertRaises(qualifier.RuntimeIdentityError):
                qualifier.load_runtime_lock(tampered_path)

    def test_qualifier_preserves_the_supplied_venv_executable_path(self) -> None:
        qualifier = load_qualifier()
        supplied = Path(sys.executable)
        self.assertEqual(supplied, qualifier.validated_executable_path(supplied))


if __name__ == "__main__":
    unittest.main()
