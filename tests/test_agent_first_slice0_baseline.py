from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from scripts.agent_first_slice0_baseline import (
    load_baseline,
    physical_line_count,
    verify_baseline,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "contracts" / "agent-first" / "worker-slice-0-baseline.json"


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
