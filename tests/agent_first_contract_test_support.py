from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


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


class AgentFirstContractTestCase(unittest.TestCase):
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
                "watched_surface_drift": "compatible_warning_after_linked_probes",
                "probe_failure": "incompatible",
                "probe_indeterminate": "indeterminate",
                "required_review": "baseline_owner_and_affected_repo_owner",
            },
            "repositories": [
                {
                    "id": "server",
                    "owner": "Pullwise Server protocol owner",
                    "frozen_head": "1" * 40,
                },
                {
                    "id": "web",
                    "owner": "Pullwise Web projection owner",
                    "frozen_head": "2" * 40,
                },
                {
                    "id": "worker",
                    "owner": "Pullwise Worker compatibility owner",
                    "frozen_head": "3" * 40,
                },
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
            "tests": [
                {
                    "id": "server.strict-v1-probe",
                    "runner_id": "server.strict-v1-probe",
                }
            ],
        }
        self.manifest_path = self.worker / "contracts" / "baseline.json"
        self.manifest_path.parent.mkdir()
        self._write_manifest()
        self._write_matching_appendix()
        self.unavailable_git_repos: set[str] = set()
        self.git_heads = {
            "server": "a" * 40,
            "web": "b" * 40,
            "worker": "c" * 40,
        }
        real_input_snapshot = baseline.input_snapshot

        def observed_snapshot(manifest, roots):
            snapshot = real_input_snapshot(manifest, roots)
            for repo_id in roots:
                available = repo_id not in self.unavailable_git_repos
                snapshot[f"head:{repo_id}"] = (
                    self.git_heads[repo_id] if available else None
                )
                snapshot[f"worktree:{repo_id}"] = "d" * 64 if available else None
            return snapshot

        def observed_head(root):
            repo_id = {
                "pullwise-server": "server",
                "pullwise-web": "web",
                "pullwise-worker": "worker",
            }[root.name]
            return (
                self.git_heads[repo_id]
                if repo_id not in self.unavailable_git_repos
                else None
            )

        self.input_snapshot_patch = mock.patch.object(
            baseline,
            "input_snapshot",
            side_effect=observed_snapshot,
        )
        self.git_head_patch = mock.patch.object(
            baseline,
            "git_head",
            side_effect=observed_head,
        )
        self.input_snapshot_patch.start()
        self.git_head_patch.start()

    def tearDown(self) -> None:
        self.git_head_patch.stop()
        self.input_snapshot_patch.stop()
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

    def _surface(self, surface_id: str) -> dict[str, object]:
        return next(item for item in self.manifest["surfaces"] if item["id"] == surface_id)
