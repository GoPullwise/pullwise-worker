from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "verify_agent_first_contract_baseline.py"
SPEC = importlib.util.spec_from_file_location("contract_baseline_verifier", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load contract baseline verifier: {SCRIPT_PATH}")
baseline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(baseline)


def canonical_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        registry_path = self.server / "registry.py"
        registry_path.write_text(
            "EVENT_TYPES = {'run_completed', 'run_started'}\n",
            encoding="utf-8",
        )
        tests_dir = self.server / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("", encoding="utf-8")
        fixture_path = tests_dir / "test_contract.py"
        fixture_path.write_text(
            "import unittest\n\n"
            "class ContractTest(unittest.TestCase):\n"
            "    def test_contract(self):\n"
            "        self.assertEqual('review-worker-protocol/v1', "
            "'review-worker-protocol/v1')\n",
            encoding="utf-8",
        )
        self.runners = {
            "server.strict-v1-probe": {
                "repo": "server",
                "runner": "python_unittest",
                "nodes": ("tests.test_contract",),
                "timeout_seconds": 30,
                "minimum_tests": 1,
            }
        }
        self.manifest = {
            "schema_id": "pullwise-contract-baseline/v1",
            "baseline_id": "strict-v1-test-baseline",
            "protocol_version": "review-worker-protocol/v1",
            "hash_profile": "sha256-utf8-lf/v1",
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
                "blocking_surface_drift": "incompatible",
                "watched_surface_drift": "indeterminate_pending_review",
                "probe_failure": "incompatible",
                "probe_indeterminate": "indeterminate",
                "required_review": "baseline_owner_and_affected_repo_owner",
            },
            "repositories": [
                {"id": "server", "owner": "Pullwise Server protocol owner", "frozen_head": "1" * 40},
                {"id": "web", "owner": "Pullwise Web projection owner", "frozen_head": "2" * 40},
                {"id": "worker", "owner": "Pullwise Worker compatibility owner", "frozen_head": "3" * 40},
            ],
            "registries": [
                {
                    "id": "server.event-types",
                    "repo": "server",
                    "path": "registry.py",
                    "surface_id": "server.registry-source",
                    "symbol": "EVENT_TYPES",
                    "ordered": False,
                    "values": ["run_completed", "run_started"],
                }
            ],
            "surfaces": [
                {
                    "id": "server.registry-source",
                    "repo": "server",
                    "path": "registry.py",
                    "roles": ["registry"],
                    "anchors": ["EVENT_TYPES"],
                    "enforcement": "watched",
                    "probe_ids": ["server.strict-v1-probe"],
                    "sha256": canonical_sha256(registry_path),
                },
                {
                    "id": "server.strict-v1-fixture",
                    "repo": "server",
                    "path": "tests/test_contract.py",
                    "roles": ["fixture"],
                    "anchors": ["review-worker-protocol/v1"],
                    "enforcement": "blocking",
                    "probe_ids": ["server.strict-v1-probe"],
                    "sha256": canonical_sha256(fixture_path),
                },
                {
                    "id": "server.strict-v1-validator",
                    "repo": "server",
                    "path": "contract.txt",
                    "roles": ["validator"],
                    "anchors": ["strict-v1-contract"],
                    "enforcement": "watched",
                    "probe_ids": ["server.strict-v1-probe"],
                    "sha256": canonical_sha256(contract_path),
                },
            ],
            "tests": [{"id": "server.strict-v1-probe", "runner_id": "server.strict-v1-probe"}],
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
            f"{baseline.render_appendix(self.manifest, runner_catalog=self.runners)}\n"
            f"{appendix['end_marker']}\n",
            encoding="utf-8",
        )

    def _verify(self, *, run_tests: bool = True) -> dict[str, object]:
        return baseline.verify_baseline(
            self.manifest,
            self.workspace,
            run_tests=run_tests,
            runner_catalog=self.runners,
        )

    def test_matching_baseline_is_compatible_and_report_is_sanitized(self) -> None:
        report = self._verify()

        self.assertEqual("compatible", report["status"])
        self.assertTrue(report["compatible"])
        self.assertTrue(report["hashes_match"])
        self.assertTrue(report["appendix_matches"])
        self.assertEqual([], report["failures"])
        self.assertEqual([], report["indeterminate_reasons"])
        self.assertEqual("passed", report["tests"][0]["status"])
        self.assertEqual(1, report["tests"][0]["observed_tests"])
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn(str(self.workspace), serialized)
        self.assertNotIn("output_tail", serialized)
        self.assertNotIn("argv", serialized)

    def test_lf_and_crlf_have_the_same_canonical_digest(self) -> None:
        path = self.server / "contract.txt"
        lf_digest = baseline.text_sha256(path)
        path.write_bytes(b"strict-v1-contract\r\n")

        self.assertEqual(lf_digest, baseline.text_sha256(path))
        self.assertEqual("compatible", self._verify()["status"])

    def test_unlisted_file_and_head_drift_do_not_block(self) -> None:
        (self.server / "unlisted.txt").write_text("changed\n", encoding="utf-8")

        report = self._verify()

        self.assertEqual("compatible", report["status"])
        self.assertEqual("informational", report["repositories"][0]["head_status"])

    def test_watched_source_drift_requires_review_even_when_its_probe_passes(self) -> None:
        (self.server / "contract.txt").write_text("strict-v1-contract changed\n", encoding="utf-8")

        report = self._verify()

        self.assertEqual("indeterminate", report["status"])
        self.assertFalse(report["hashes_match"])
        self.assertEqual("watched_surface_drift", report["warnings"][0]["code"])
        self.assertEqual("server.strict-v1-validator", report["warnings"][0]["surface_id"])
        self.assertIn(
            "watched_surface_drift",
            {item["code"] for item in report["indeterminate_reasons"]},
        )

    def test_blocking_fixture_drift_is_incompatible(self) -> None:
        fixture = self.server / "tests" / "test_contract.py"
        fixture.write_text(fixture.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

        report = self._verify()

        self.assertEqual("incompatible", report["status"])
        self.assertFalse(report["compatible"])
        self.assertEqual("blocking_surface_drift", report["failures"][0]["code"])

    def test_failed_probe_is_incompatible_without_raw_output(self) -> None:
        fixture = self.server / "tests" / "test_contract.py"
        failing = fixture.read_text(encoding="utf-8").replace(
            "self.assertEqual('review-worker-protocol/v1', 'review-worker-protocol/v1')",
            "self.fail('secret fixture drift')",
        )
        fixture.write_text(failing, encoding="utf-8")
        self.manifest["surfaces"][0]["sha256"] = canonical_sha256(fixture)
        self._write_matching_appendix()

        report = self._verify()

        self.assertEqual("incompatible", report["status"])
        self.assertEqual("failed", report["tests"][0]["status"])
        self.assertRegex(report["tests"][0]["output_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("secret fixture drift", json.dumps(report))
        self.assertEqual("probe_failed", report["failures"][-1]["code"])

    def test_not_running_a_required_probe_makes_watched_drift_indeterminate(self) -> None:
        (self.server / "contract.txt").write_text("strict-v1-contract changed\n", encoding="utf-8")

        report = self._verify(run_tests=False)

        self.assertEqual("indeterminate", report["status"])
        self.assertEqual("probe_not_run", report["indeterminate_reasons"][0]["code"])

    def test_probe_input_mutation_is_indeterminate_even_when_the_probe_passes(self) -> None:
        fixture = self.server / "tests" / "test_contract.py"
        fixture.write_text(
            "from pathlib import Path\n"
            "import unittest\n\n"
            "PROTOCOL = 'review-worker-protocol/v1'\n\n"
            "class ContractTest(unittest.TestCase):\n"
            "    def test_contract(self):\n"
            "        Path('contract.txt').write_text('strict-v1-contract changed\\n')\n",
            encoding="utf-8",
        )
        self.manifest["surfaces"][0]["sha256"] = canonical_sha256(fixture)
        self._write_matching_appendix()

        report = self._verify()

        self.assertEqual("passed", report["tests"][0]["status"])
        self.assertEqual("indeterminate", report["status"])
        self.assertIn(
            "inputs_changed_during_probe",
            {item["code"] for item in report["indeterminate_reasons"]},
        )

    def test_appendix_drift_is_incompatible(self) -> None:
        appendix_path = self.worker / self.manifest["appendix"]["path"]
        appendix_path.write_text("stale appendix\n", encoding="utf-8")

        report = self._verify(run_tests=False)

        self.assertEqual("incompatible", report["status"])
        self.assertFalse(report["appendix_matches"])
        self.assertEqual("appendix_drift", report["failures"][-1]["code"])

    def test_registry_value_drift_is_incompatible_and_located(self) -> None:
        (self.server / "registry.py").write_text(
            "EVENT_TYPES = {'run_failed', 'run_started'}\n",
            encoding="utf-8",
        )

        report = self._verify()

        self.assertEqual("incompatible", report["status"])
        self.assertEqual("registry_mismatch", report["failures"][0]["code"])
        self.assertEqual("server.event-types", report["failures"][0]["registry_id"])
        self.assertEqual("drift", report["registries"][0]["status"])

    def test_candidate_is_read_only_and_uses_canonical_hashes(self) -> None:
        original = copy.deepcopy(self.manifest)
        path = self.server / "contract.txt"
        path.write_bytes(b"strict-v1-contract-v2\r\n")

        candidate = baseline.create_candidate(
            self.manifest, self.workspace, runner_catalog=self.runners
        )

        self.assertEqual(original, self.manifest)
        surface = next(item for item in candidate["surfaces"] if item["path"] == "contract.txt")
        self.assertEqual(baseline.text_sha256(path), surface["sha256"])
        self.assertEqual("1" * 40, candidate["repositories"][0]["frozen_head"])

    def test_manifest_rejects_paths_collisions_unknown_fields_and_unknown_runners(self) -> None:
        cases = []
        escaped = copy.deepcopy(self.manifest)
        escaped["surfaces"][0]["path"] = "../outside"
        cases.append(escaped)
        reserved = copy.deepcopy(self.manifest)
        reserved["surfaces"][0]["path"] = "tests/CON.txt"
        cases.append(reserved)
        duplicate = copy.deepcopy(self.manifest)
        duplicate["surfaces"].append(copy.deepcopy(duplicate["surfaces"][0]))
        cases.append(duplicate)
        bad_hash = copy.deepcopy(self.manifest)
        bad_hash["surfaces"][0]["sha256"] = "ABC"
        cases.append(bad_hash)
        unknown = copy.deepcopy(self.manifest)
        unknown["surfaces"][0]["surprise"] = True
        cases.append(unknown)
        unknown_runner = copy.deepcopy(self.manifest)
        unknown_runner["tests"][0]["runner_id"] = "server.user-controlled-command"
        cases.append(unknown_runner)

        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(baseline.ManifestError):
                baseline.validate_manifest(payload, runner_catalog=self.runners)

    def test_runner_command_comes_only_from_the_fixed_catalog(self) -> None:
        python_argv = baseline.build_test_argv(
            "server.strict-v1-probe",
            runner_catalog=self.runners,
            python_executable="/python",
            npm_executable="/npm",
        )

        self.assertEqual(
            ["/python", "-B", "-m", "unittest", "tests.test_contract"],
            python_argv,
        )
        self.assertEqual(
            {"id": "server.strict-v1-probe", "runner_id": "server.strict-v1-probe"},
            self.manifest["tests"][0],
        )

    def test_cli_emits_one_json_report_with_three_stable_exit_codes(self) -> None:
        def invoke() -> tuple[int, dict[str, object]]:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = baseline.main(
                    [
                        "check",
                        "--manifest",
                        str(self.manifest_path),
                        "--workspace-root",
                        str(self.workspace),
                    ],
                    runner_catalog=self.runners,
                )
            return exit_code, json.loads(output.getvalue())

        exit_code, report = invoke()
        self.assertEqual(0, exit_code)
        self.assertEqual("compatible", report["status"])

        appendix_path = self.worker / self.manifest["appendix"]["path"]
        appendix_path.write_text("stale\n", encoding="utf-8")
        exit_code, report = invoke()
        self.assertEqual(1, exit_code)
        self.assertEqual("incompatible", report["status"])

        self.manifest["schema_id"] = "unknown/v1"
        self._write_manifest()
        exit_code, report = invoke()
        self.assertEqual(2, exit_code)
        self.assertEqual("indeterminate", report["status"])
        self.assertEqual("manifest_invalid", report["error_kind"])


if __name__ == "__main__":
    unittest.main()
