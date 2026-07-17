from __future__ import annotations

from contextlib import redirect_stdout
import copy
import io
import json
import unittest

from agent_first_contract_test_support import (
    AgentFirstContractTestCase,
    baseline,
    canonical_sha256,
)


class AgentFirstContractBaselineTest(AgentFirstContractTestCase):
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

    def test_unavailable_git_observation_is_indeterminate(self) -> None:
        self.unavailable_git_repos.add("server")

        report = self._verify()

        self.assertEqual("indeterminate", report["status"])
        self.assertIn(
            {"code": "repository_observation_unavailable", "repo": "server"},
            report["indeterminate_reasons"],
        )

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

    def test_deterministic_failure_precedes_indeterminate_probe_state(self) -> None:
        fixture = self.server / "tests" / "test_contract.py"
        fixture.write_text(
            fixture.read_text(encoding="utf-8") + "# drift" + chr(10),
            encoding="utf-8",
        )

        report = self._verify(run_tests=False)

        self.assertEqual("incompatible", report["status"])
        self.assertIn(
            "blocking_surface_drift",
            {item["code"] for item in report["failures"]},
        )
        self.assertIn(
            "probe_not_run",
            {item["code"] for item in report["indeterminate_reasons"]},
        )

    def test_failed_probe_is_incompatible_without_raw_output(self) -> None:
        fixture = self.server / "tests" / "test_contract.py"
        failing = fixture.read_text(encoding="utf-8").replace(
            "self.assertEqual('review-worker-protocol/v1', 'review-worker-protocol/v1')",
            "self.fail('secret fixture drift')",
        )
        fixture.write_text(failing, encoding="utf-8")
        self._surface("server.strict-v1-fixture")["sha256"] = canonical_sha256(fixture)
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
        self._surface("server.strict-v1-fixture")["sha256"] = canonical_sha256(fixture)
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
        before = {
            item.relative_to(self.workspace).as_posix(): item.read_bytes()
            for item in self.workspace.rglob("*")
            if item.is_file()
        }

        candidate = baseline.create_candidate(
            self.manifest, self.workspace, runner_catalog=self.runners
        )
        after = {
            item.relative_to(self.workspace).as_posix(): item.read_bytes()
            for item in self.workspace.rglob("*")
            if item.is_file()
        }

        self.assertEqual(original, self.manifest)
        self.assertEqual(before, after)
        surface = next(item for item in candidate["surfaces"] if item["path"] == "contract.txt")
        self.assertEqual(baseline.text_sha256(path), surface["sha256"])
        self.assertEqual(
            baseline.git_head(self.server),
            candidate["repositories"][0]["frozen_head"],
        )

    def test_candidate_rejects_evidence_for_a_different_input_snapshot(self) -> None:
        evidence = self._verify()
        (self.server / "contract.txt").write_text(
            "strict-v1-contract changed" + chr(10),
            encoding="utf-8",
        )

        with self.assertRaises(baseline.BaselineEnvironmentError):
            baseline.create_candidate(
                self.manifest,
                self.workspace,
                runner_catalog=self.runners,
                evidence_report=evidence,
            )

    def test_candidate_rejects_structurally_incomplete_evidence(self) -> None:
        evidence = copy.deepcopy(self._verify())
        evidence["tests"] = []

        with self.assertRaises(baseline.BaselineEnvironmentError):
            baseline.create_candidate(
                self.manifest,
                self.workspace,
                runner_catalog=self.runners,
                evidence_report=evidence,
            )

    def test_candidate_cli_includes_passing_probe_evidence(self) -> None:
        output = io.StringIO()
        before = self.manifest_path.read_bytes()
        with redirect_stdout(output):
            exit_code = baseline.main(
                [
                    "candidate",
                    "--manifest",
                    str(self.manifest_path),
                    "--workspace-root",
                    str(self.workspace),
                ],
                runner_catalog=self.runners,
            )
        payload = json.loads(output.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual("pullwise-contract-baseline-candidate/v1", payload["schema_id"])
        self.assertEqual("passed", payload["probe_evidence"]["tests"][0]["status"])
        self.assertRegex(payload["candidate_manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            payload["probe_evidence"]["input_snapshot_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertIn("candidate_manifest", payload)
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_candidate_refuses_failed_probe_evidence(self) -> None:
        fixture = self.server / "tests" / "test_contract.py"
        fixture.write_text(
            fixture.read_text(encoding="utf-8").replace(
                "self.assertEqual('review-worker-protocol/v1', 'review-worker-protocol/v1')",
                "self.fail('contract failed: review-worker-protocol/v1')",
            ),
            encoding="utf-8",
        )
        self._surface("server.strict-v1-fixture")["sha256"] = canonical_sha256(fixture)
        self._write_matching_appendix()

        with self.assertRaises(baseline.BaselineEnvironmentError):
            baseline.create_candidate(
                self.manifest, self.workspace, runner_catalog=self.runners
            )

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
