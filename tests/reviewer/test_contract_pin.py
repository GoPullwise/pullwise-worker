from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[2]
PIN_PATH = REPO_ROOT / "reviewer-contract-pin.json"
CONSUMER_PATH = REPO_ROOT / "pullwise_worker" / "_generated_reviewer_contract.py"
CHECKER_PATH = REPO_ROOT / "scripts" / "check_reviewer_contract_pin.py"


def copy_wheel_source(destination: Path) -> None:
    destination.mkdir()
    for filename in ("pyproject.toml", "MANIFEST.in", "reviewer-contract-pin.json"):
        shutil.copy2(REPO_ROOT / filename, destination / filename)
    shutil.copytree(
        REPO_ROOT / "pullwise_worker",
        destination / "pullwise_worker",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copytree(REPO_ROOT / "deploy", destination / "deploy")
    contract_destination = destination / "contracts" / "agent-task" / "v1"
    contract_destination.parent.mkdir(parents=True)
    shutil.copytree(
        REPO_ROOT / "contracts" / "agent-task" / "v1",
        contract_destination,
    )


class ReviewerContractPinTest(unittest.TestCase):
    def test_pin_binds_the_vendored_consumer_to_the_frozen_manifest(self) -> None:
        pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
        spec = importlib.util.spec_from_file_location(
            "vendored_reviewer_contract", CONSUMER_PATH
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)

        self.assertEqual(pin["schema_id"], "pullwise-reviewer-contract-pin/v1")
        self.assertEqual(pin["contract_version"], module.CONTRACT_VERSION)
        self.assertEqual(pin["canonicalization"], module.CANONICALIZATION)
        self.assertEqual(pin["manifest_digest"], module.MANIFEST_DIGEST)
        self.assertEqual(
            pin["consumer_path"],
            "pullwise_worker/_generated_reviewer_contract.py",
        )
        self.assertEqual(
            pin["consumer_sha256"],
            "sha256:" + hashlib.sha256(CONSUMER_PATH.read_bytes()).hexdigest(),
        )

    def test_checker_passes_without_reading_server_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reviewer-contract-pin-") as scratch:
            isolated = Path(scratch)
            (isolated / "pullwise_worker").mkdir()
            (isolated / "scripts").mkdir()
            shutil.copy2(PIN_PATH, isolated / PIN_PATH.name)
            shutil.copy2(
                CONSUMER_PATH,
                isolated / "pullwise_worker" / CONSUMER_PATH.name,
            )
            shutil.copy2(CHECKER_PATH, isolated / "scripts" / CHECKER_PATH.name)

            result = subprocess.run(
                [
                    sys.executable,
                    str(isolated / "scripts" / CHECKER_PATH.name),
                    "--repo-root",
                    str(isolated),
                ],
                cwd=isolated,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok: reviewer contract pin is pristine", result.stdout)

    def test_wheel_contains_the_vendored_consumer_and_pin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reviewer-contract-wheel-") as scratch:
            scratch_root = Path(scratch)
            source_root = scratch_root / "source"
            wheel_root = scratch_root / "dist"
            copy_wheel_source(source_root)
            wheel_root.mkdir()
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-build-isolation",
                    "--no-deps",
                    "--wheel-dir",
                    str(wheel_root),
                    str(source_root),
                ],
                cwd=source_root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            wheels = tuple(wheel_root.glob("pullwise_worker-*.whl"))
            self.assertEqual(len(wheels), 1, wheels)
            with zipfile.ZipFile(wheels[0]) as wheel:
                names = set(wheel.namelist())
                self.assertIn(
                    "pullwise_worker/_generated_reviewer_contract.py",
                    names,
                )
                pin_names = [
                    name
                    for name in names
                    if name.endswith(
                        "share/pullwise-worker/contracts/pullwise-review/v1/"
                        "reviewer-contract-pin.json"
                    )
                ]
                self.assertEqual(len(pin_names), 1, pin_names)
                self.assertEqual(wheel.read(pin_names[0]), PIN_PATH.read_bytes())


if __name__ == "__main__":
    unittest.main()
