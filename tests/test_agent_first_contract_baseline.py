from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "verify_agent_first_contract_baseline.py"
SPEC = importlib.util.spec_from_file_location(
    "verify_agent_first_contract_baseline",
    SCRIPT_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load contract baseline verifier: {SCRIPT_PATH}")
baseline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(baseline)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AgentFirstContractBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pullwise-contract-baseline-")
        self.workspace = Path(self.temp_dir.name)
        self.server = self.workspace / "pullwise-server"
        self.web = self.workspace / "pullwise-web"
        self.worker = self.workspace / "pullwise-worker"
        for root in (self.server, self.web, self.worker):
            root.mkdir(parents=True)

        contract_path = self.server / "contract.txt"
        contract_path.write_text("strict-v1-contract\n", encoding="utf-8")
        tests_dir = self.server / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("", encoding="utf-8")
        (tests_dir / "test_contract.py").write_text(
            "import unittest\n\n"
            "class ContractTest(unittest.TestCase):\n"
            "    def test_contract(self):\n"
            "        self.assertEqual('review-worker-protocol/v1', "
            "'review-worker-protocol/v1')\n",
            encoding="utf-8",
        )

        self.manifest = {
            "schema_id": "pullwise-contract-baseline/v1",
            "baseline_id": "strict-v1-test-baseline",
            "protocol_version": "review-worker-protocol/v1",
            "hash_profile": "sha256-raw-bytes/v1",
            "baseline_owner": "Pullwise Worker compatibility owner",
            "appendix": {
                "repo": "worker",
                "path": "docs/mvp.md",
                "start_marker": "<!-- BEGIN GENERATED LEGACY V1 BASELINE -->",
                "end_marker": "<!-- END GENERATED LEGACY V1 BASELINE -->",
            },
            "compatibility_policy": {
                "head_drift": "informational",
                "unlisted_path_drift": "ignored",
                "surface_hash_drift": "incompatible_pending_review",
                "test_failure": "incompatible",
                "required_review": "baseline_owner_and_affected_repo_owner",
            },
            "repositories": [
                {
                    "id": "server",
                    "path": "pullwise-server",
                    "owner": "Pullwise Server protocol owner",
                    "frozen_head": "1" * 40,
                },
                {
                    "id": "web",
                    "path": "pullwise-web",
                    "owner": "Pullwise Web projection owner",
                    "frozen_head": "2" * 40,
                },
                {
                    "id": "worker",
                    "path": "pullwise-worker",
                    "owner": "Pullwise Worker compatibility owner",
                    "frozen_head": "3" * 40,
                },
            ],
            "surfaces": [
                {
                    "id": "server.strict-v1-validator",
                    "repo": "server",
                    "path": "contract.txt",
                    "roles": ["validator"],
                    "anchors": ["strict-v1-contract"],
                    "sha256": file_sha256(contract_path),
                }
            ],
            "tests": [
                {
                    "id": "server.strict-v1-fixtures",
                    "repo": "server",
                    "runner": "python_unittest",
                    "nodes": ["tests.test_contract"],
                    "timeout_seconds": 30,
                }
            ],
        }
        self.manifest_path = self.worker / "contracts" / "baseline.json"
        self.manifest_path.parent.mkdir()
        self._write_manifest()
        self._write_matching_appendix()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_manifest(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_matching_appendix(self) -> None:
        appendix = self.manifest["appendix"]
        path = self.worker / appendix["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# MVP\n\n"
            f"{appendix['start_marker']}\n"
            f"{baseline.render_appendix(self.manifest)}\n"
            f"{appendix['end_marker']}\n",
            encoding="utf-8",
        )

    def test_matching_surfaces_passing_tests_and_synced_appendix_are_compatible(self) -> None:
        report = baseline.verify_baseline(self.manifest, self.workspace, run_tests=True)

        self.assertTrue(report["compatible"])
        self.assertTrue(report["hashes_match"])
        self.assertTrue(report["appendix_matches"])
        self.assertEqual([], report["failures"])
        self.assertEqual("passed", report["tests"][0]["status"])
        self.assertIsNone(report["repositories"][0]["current_head"])

    def test_unlisted_file_and_unavailable_git_head_do_not_block(self) -> None:
        (self.server / "unlisted.txt").write_text("changed\n", encoding="utf-8")

        report = baseline.verify_baseline(self.manifest, self.workspace, run_tests=True)

        self.assertTrue(report["compatible"])
        self.assertEqual("informational", report["repositories"][0]["head_status"])

    def test_surface_drift_and_missing_anchor_are_located(self) -> None:
        (self.server / "contract.txt").write_text("changed\n", encoding="utf-8")

        report = baseline.verify_baseline(self.manifest, self.workspace, run_tests=False)

        self.assertFalse(report["compatible"])
        self.assertFalse(report["hashes_match"])
        self.assertEqual(
            {"anchor_missing", "surface_hash_mismatch"},
            {failure["code"] for failure in report["failures"]},
        )
        self.assertTrue(
            all(
                failure["surface_id"] == "server.strict-v1-validator"
                for failure in report["failures"]
            )
        )

    def test_missing_surface_fails_closed_but_is_not_a_manifest_parse_error(self) -> None:
        (self.server / "contract.txt").unlink()

        report = baseline.verify_baseline(self.manifest, self.workspace, run_tests=False)

        self.assertFalse(report["compatible"])
        self.assertEqual("surface_missing", report["failures"][0]["code"])

    def test_test_failure_is_incompatible(self) -> None:
        (self.server / "tests" / "test_contract.py").write_text(
            "import unittest\n\n"
            "class ContractTest(unittest.TestCase):\n"
            "    def test_contract(self):\n"
            "        self.fail('fixture drift')\n",
            encoding="utf-8",
        )

        report = baseline.verify_baseline(self.manifest, self.workspace, run_tests=True)

        self.assertFalse(report["compatible"])
        self.assertEqual("failed", report["tests"][0]["status"])
        self.assertIn("fixture drift", report["tests"][0]["output_tail"])
        self.assertEqual("test_failed", report["failures"][-1]["code"])

    def test_appendix_drift_is_incompatible(self) -> None:
        appendix_path = self.worker / self.manifest["appendix"]["path"]
        appendix_path.write_text("stale appendix\n", encoding="utf-8")

        report = baseline.verify_baseline(self.manifest, self.workspace, run_tests=False)

        self.assertFalse(report["compatible"])
        self.assertFalse(report["appendix_matches"])
        self.assertEqual("appendix_drift", report["failures"][-1]["code"])

    def test_candidate_refreshes_heads_and_hashes_without_mutating_input(self) -> None:
        original = copy.deepcopy(self.manifest)
        (self.server / "contract.txt").write_text("strict-v1-contract-v2\n", encoding="utf-8")

        candidate = baseline.create_candidate(self.manifest, self.workspace)

        self.assertEqual(original, self.manifest)
        self.assertEqual(
            file_sha256(self.server / "contract.txt"),
            candidate["surfaces"][0]["sha256"],
        )
        self.assertEqual("1" * 40, candidate["repositories"][0]["frozen_head"])

    def test_manifest_validation_rejects_unsafe_or_ambiguous_input(self) -> None:
        cases = []
        escaped = copy.deepcopy(self.manifest)
        escaped["surfaces"][0]["path"] = "../outside"
        cases.append(escaped)
        duplicate = copy.deepcopy(self.manifest)
        duplicate["surfaces"].append(copy.deepcopy(duplicate["surfaces"][0]))
        cases.append(duplicate)
        bad_hash = copy.deepcopy(self.manifest)
        bad_hash["surfaces"][0]["sha256"] = "ABC"
        cases.append(bad_hash)
        unknown = copy.deepcopy(self.manifest)
        unknown["surfaces"][0]["surprise"] = True
        cases.append(unknown)
        unsafe_node = copy.deepcopy(self.manifest)
        unsafe_node["tests"][0]["nodes"] = ["tests.test_contract && whoami"]
        cases.append(unsafe_node)

        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(baseline.ManifestError):
                baseline.validate_manifest(payload)

    def test_test_argv_is_exact_and_never_uses_a_shell_string(self) -> None:
        python_argv = baseline.build_test_argv(
            self.manifest["tests"][0],
            python_executable="/python",
            npm_executable="/npm",
        )
        npm_test = {
            "id": "web.projection-fixtures",
            "repo": "web",
            "runner": "npm_test",
            "nodes": ["src/lib/pullwise-data.test.js"],
            "timeout_seconds": 30,
        }

        self.assertEqual(
            ["/python", "-B", "-m", "unittest", "tests.test_contract"],
            python_argv,
        )
        self.assertEqual(
            ["/npm", "test", "--", "src/lib/pullwise-data.test.js"],
            baseline.build_test_argv(
                npm_test,
                python_executable="/python",
                npm_executable="/npm",
            ),
        )
        self.assertTrue(all(isinstance(part, str) for part in python_argv))

    def test_cli_emits_one_json_report_and_uses_stable_exit_codes(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = baseline.main(
                [
                    "check",
                    "--manifest",
                    str(self.manifest_path),
                    "--workspace-root",
                    str(self.workspace),
                ]
            )
        report = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(report["compatible"])

        self.manifest["schema_id"] = "unknown/v1"
        self._write_manifest()
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = baseline.main(
                [
                    "check",
                    "--manifest",
                    str(self.manifest_path),
                    "--workspace-root",
                    str(self.workspace),
                ]
            )
        report = json.loads(output.getvalue())
        self.assertEqual(2, exit_code)
        self.assertEqual("manifest_invalid", report["error_kind"])


if __name__ == "__main__":
    unittest.main()
