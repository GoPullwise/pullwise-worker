from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.agent_first_slice0_baseline import (
    BaselineFormatError,
    load_baseline,
    physical_line_count,
    verify_baseline,
)
from scripts.agent_first_slice0_manifest import validate_baseline


REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "contracts" / "agent-first" / "worker-slice-0-baseline.json"
GENERATED_PATH = "pullwise_worker/_generated_agent_task_contract.py"
GENERATED_MARKER = (
    '\"\"\"Generated from the Server-owned Agent-First bundle; do not edit.\"\"\"'
)
GENERATED_PROVENANCE = (
    "pullwise-server@43ca421c862772a2e000d617ef0c2f1b83759590:"
    "pullwise_server/agent_first_contract_bundle_python.py"
)


def _synthetic_v2_baseline(generated_data: bytes) -> dict[str, object]:
    return {
        "schema_id": "pullwise-agent-first-slice-0-baseline/v2",
        "baseline_id": "test",
        "captured_head": "0" * 40,
        "line_count_profile": "physical-lf/v1",
        "document": {
            "path": "code-map.md",
            "start_marker": "<!-- BEGIN -->",
            "end_marker": "<!-- END -->",
        },
        "pipeline": {
            "path": "pipeline.py",
            "symbol": "PIPELINE_PHASES",
            "values": [["only_phase", 100]],
        },
        "code_map": [
            {
                "id": "pipeline",
                "paths": [{"path": "pipeline.py", "anchors": ["PIPELINE_PHASES"]}],
                "current_responsibilities": "Synthetic pipeline.",
                "boundary": "Synthetic boundary.",
                "candidate_extraction_seam": "Synthetic seam.",
            }
        ],
        "generated_file_exceptions": [
            {
                "path": GENERATED_PATH,
                "physical_lines": physical_line_count(generated_data),
                "sha256": hashlib.sha256(generated_data).hexdigest(),
                "marker": GENERATED_MARKER,
                "provenance": GENERATED_PROVENANCE,
                "reason": "Synthetic generated exception.",
                "considered_split_seam": "Synthetic generator seam.",
                "owner": "Synthetic owner.",
                "removal_condition": "Remove at 400 lines or fewer.",
            }
        ],
        "file_baselines": [
            {
                "path": "known.py",
                "kind": "production",
                "classification": "review_trigger_existing",
                "physical_lines": 401,
                "anchors": ["pass"],
                "current_responsibilities": "Synthetic known file.",
                "candidate_extraction_seam": "Synthetic seam.",
            }
        ],
    }


def _verify_generated_transition(
    baseline: dict[str, object], generated_data: bytes
) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        generated = root / GENERATED_PATH
        generated.parent.mkdir(parents=True)
        generated.write_bytes(generated_data)
        (root / "known.py").write_text(
            "pass\n" * 401, encoding="utf-8", newline="\n"
        )
        (root / "pipeline.py").write_text(
            "PIPELINE_PHASES = (('only_phase', 100),)\n",
            encoding="utf-8",
            newline="\n",
        )
        return verify_baseline(
            baseline,
            root,
            tracked_paths=(GENERATED_PATH, "known.py", "pipeline.py"),
            check_document=False,
        )


class AgentFirstSlice0BaselineTest(unittest.TestCase):
    def test_current_repository_matches_slice0_baseline(self) -> None:
        baseline = load_baseline(BASELINE_PATH)

        report = verify_baseline(baseline, REPO_ROOT)

        self.assertEqual("compatible", report["status"])
        self.assertEqual([], report["failures"])
        self.assertTrue(report["document_matches"])
        self.assertEqual(30, report["pipeline_phase_count"])
        self.assertEqual(11, report["oversized_legacy_count"])
        self.assertEqual(7, report["review_trigger_count"])
        self.assertEqual(1, report["generated_exception_count"])

    def test_generated_exception_is_exact_digest_marker_and_provenance(self) -> None:
        baseline = load_baseline(BASELINE_PATH)
        generated_exception = baseline["generated_file_exceptions"][0]
        changed = copy.deepcopy(baseline)
        changed["generated_file_exceptions"][0]["physical_lines"] = 500
        changed["generated_file_exceptions"][0]["sha256"] = "0" * 64

        report = verify_baseline(changed, REPO_ROOT, check_document=False)

        self.assertIn(
            {
                "code": "generated_exception_line_count_mismatch",
                "path": generated_exception["path"],
                "expected": 500,
                "actual": generated_exception["physical_lines"],
            },
            report["failures"],
        )
        self.assertIn(
            {
                "code": "generated_exception_digest_mismatch",
                "path": generated_exception["path"],
            },
            report["failures"],
        )

        changed = copy.deepcopy(baseline)
        changed["generated_file_exceptions"][0]["physical_lines"] = 400
        with self.assertRaisesRegex(
            BaselineFormatError,
            r"generated_file_exceptions\[0\]\.physical_lines",
        ):
            verify_baseline(changed, REPO_ROOT, check_document=False)

        for field, value in (
            ("path", "pullwise_worker/another_generated.py"),
            ("marker", "Generated by an untrusted producer."),
            ("provenance", "untrusted"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(baseline)
                changed["generated_file_exceptions"][0][field] = value

                with self.assertRaisesRegex(
                    BaselineFormatError,
                    rf"generated_file_exceptions\[0\]\.{field}",
                ):
                    verify_baseline(changed, REPO_ROOT, check_document=False)

    def test_generated_exception_remains_exact_at_500_lines(self) -> None:
        generated_data = (GENERATED_MARKER + "\n" + "pass\n" * 499).encode("utf-8")
        baseline = _synthetic_v2_baseline(generated_data)

        report = _verify_generated_transition(baseline, generated_data)

        self.assertEqual("compatible", report["status"])
        self.assertEqual([], report["failures"])

    def test_500_line_wrapper_without_exception_fails_closed(self) -> None:
        generated_data = (GENERATED_MARKER + "\n" + "pass\n" * 499).encode("utf-8")
        baseline = _synthetic_v2_baseline(generated_data)
        baseline["generated_file_exceptions"] = []

        report = _verify_generated_transition(baseline, generated_data)

        self.assertEqual("incompatible", report["status"])
        self.assertIn(
            {"code": "trigger_file_missing_from_baseline", "path": GENERATED_PATH},
            report["failures"],
        )

    def test_400_line_wrapper_no_longer_requires_exception(self) -> None:
        generated_data = (GENERATED_MARKER + "\n" + "pass\n" * 399).encode("utf-8")
        baseline = _synthetic_v2_baseline(generated_data)
        baseline["generated_file_exceptions"] = []

        report = _verify_generated_transition(baseline, generated_data)

        self.assertEqual("compatible", report["status"])
        self.assertEqual([], report["failures"])

    def test_v1_manifest_rejects_v2_generated_exception_key(self) -> None:
        generated_data = (GENERATED_MARKER + "\n" + "pass\n" * 499).encode("utf-8")
        baseline = _synthetic_v2_baseline(generated_data)
        baseline["schema_id"] = "pullwise-agent-first-slice-0-baseline/v1"

        with self.assertRaisesRegex(BaselineFormatError, r"^baseline:keys$"):
            validate_baseline(baseline)

    def test_line_count_growth_is_incompatible(self) -> None:
        baseline = load_baseline(BASELINE_PATH)
        changed = copy.deepcopy(baseline)
        changed["file_baselines"][0]["physical_lines"] -= 1

        report = verify_baseline(changed, REPO_ROOT, check_document=False)

        self.assertEqual("incompatible", report["status"])
        self.assertIn(
            {
                "code": "physical_line_count_drift",
                "path": changed["file_baselines"][0]["path"],
                "expected": changed["file_baselines"][0]["physical_lines"],
                "actual": changed["file_baselines"][0]["physical_lines"] + 1,
            },
            report["failures"],
        )

    def test_unregistered_file_above_review_trigger_is_incompatible(self) -> None:
        baseline = {
            "schema_id": "pullwise-agent-first-slice-0-baseline/v1",
            "baseline_id": "test",
            "captured_head": "0" * 40,
            "line_count_profile": "physical-lf/v1",
            "document": {
                "path": "code-map.md",
                "start_marker": "<!-- BEGIN -->",
                "end_marker": "<!-- END -->",
            },
            "pipeline": {
                "path": "pipeline.py",
                "symbol": "PIPELINE_PHASES",
                "values": [["only_phase", 100]],
            },
            "code_map": [
                {
                    "id": "pipeline",
                    "paths": [{"path": "pipeline.py", "anchors": ["PIPELINE_PHASES"]}],
                    "current_responsibilities": "Synthetic pipeline.",
                    "boundary": "Synthetic boundary.",
                    "candidate_extraction_seam": "Synthetic seam.",
                }
            ],
            "file_baselines": [
                {
                    "path": "known.py",
                    "kind": "production",
                    "classification": "review_trigger_existing",
                    "physical_lines": 401,
                    "anchors": ["pass"],
                    "current_responsibilities": "Synthetic known file.",
                    "candidate_extraction_seam": "Synthetic seam.",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidate = root / "new_module.py"
            candidate.write_text("pass\n" * 401, encoding="utf-8", newline="\n")
            (root / "known.py").write_text(
                "pass\n" * 401, encoding="utf-8", newline="\n"
            )
            (root / "pipeline.py").write_text(
                "PIPELINE_PHASES = (('only_phase', 100),)\n",
                encoding="utf-8",
                newline="\n",
            )

            report = verify_baseline(
                baseline,
                root,
                tracked_paths=("known.py", "new_module.py", "pipeline.py"),
                check_document=False,
            )

        self.assertEqual("incompatible", report["status"])
        self.assertIn(
            {"code": "trigger_file_missing_from_baseline", "path": "new_module.py"},
            report["failures"],
        )

    def test_physical_line_count_handles_empty_and_unterminated_files(self) -> None:
        self.assertEqual(0, physical_line_count(b""))
        self.assertEqual(1, physical_line_count(b"one"))
        self.assertEqual(1, physical_line_count(b"one\n"))
        self.assertEqual(2, physical_line_count(b"one\ntwo"))

    def test_slice0_machine_entrypoint_is_documented(self) -> None:
        command = "python scripts/agent_first_slice0_baseline.py check --repo-root ."
        manifest = "contracts/agent-first/worker-slice-0-baseline.json"
        for relative in (
            "AGENTS.md",
            "docs/agent-first-worker-mvp-implementation-design.md",
        ):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertIn(command, text)
                self.assertIn(manifest, text)


if __name__ == "__main__":
    unittest.main()
