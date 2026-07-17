from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from scripts.agent_first_slice0_baseline import (
    BaselineFormatError,
    validate_baseline,
    verify_baseline,
)
from scripts.agent_first_slice0_render import render_document


def _file_entry(path: str, lines: int) -> dict[str, object]:
    return {
        "path": path,
        "kind": "production",
        "classification": "oversized_legacy" if lines > 600 else "review_trigger_existing",
        "physical_lines": lines,
        "anchors": ["pass"],
        "current_responsibilities": "Synthetic current responsibility.",
        "candidate_extraction_seam": "Synthetic extraction seam.",
    }


def _baseline(*entries: dict[str, object]) -> dict[str, object]:
    file_baselines = list(entries or (_file_entry("known.py", 401),))
    file_baselines.sort(key=lambda item: (-int(item["physical_lines"]), str(item["path"])))
    return {
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
        "file_baselines": file_baselines,
    }


def _write_common(root: Path, *, known_lines: int = 401, pipeline: str | None = None) -> None:
    (root / "known.py").write_text("pass\n" * known_lines, encoding="utf-8", newline="\n")
    (root / "pipeline.py").write_text(
        pipeline or "PIPELINE_PHASES = (('only_phase', 100),)\n",
        encoding="utf-8",
        newline="\n",
    )


class AgentFirstSlice0GateAdversarialTest(unittest.TestCase):
    def test_windows_and_drive_qualified_manifest_paths_are_rejected(self) -> None:
        for unsafe in ("..\\outside.py", "C:/outside.py", "nested\\file.py"):
            changed = copy.deepcopy(_baseline())
            changed["document"]["path"] = unsafe
            with self.subTest(path=unsafe), self.assertRaises(BaselineFormatError):
                validate_baseline(changed)

    def test_pipeline_registry_rejects_progress_type_coercion(self) -> None:
        for progress in ("'100'", "100.0", "True"):
            with self.subTest(progress=progress), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_common(
                    root,
                    pipeline=f"PIPELINE_PHASES = (('only_phase', {progress}),)\n",
                )
                report = verify_baseline(
                    _baseline(),
                    root,
                    tracked_paths=("known.py", "pipeline.py"),
                    check_document=False,
                )
                self.assertIn("pipeline_registry_drift", {item["code"] for item in report["failures"]})

    def test_pipeline_registry_rejects_effective_reassignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_common(
                root,
                pipeline=(
                    "PIPELINE_PHASES = (('only_phase', 100),)\n"
                    "PIPELINE_PHASES = (('changed_phase', 99),)\n"
                ),
            )
            report = verify_baseline(
                _baseline(),
                root,
                tracked_paths=("known.py", "pipeline.py"),
                check_document=False,
            )
        self.assertIn("pipeline_registry_drift", {item["code"] for item in report["failures"]})

    def test_broad_code_suffixes_and_extensionless_executable_are_gated(self) -> None:
        candidate_paths = (
            "new.go",
            "new.rs",
            "new.java",
            "new.cs",
            "new.c",
            "new.cpp",
            "new.bat",
            "new.cmd",
            "new.vue",
            "new.svelte",
            "tool",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_common(root)
            for path in candidate_paths:
                (root / path).write_text("pass\n" * 401, encoding="utf-8", newline="\n")
            report = verify_baseline(
                _baseline(),
                root,
                tracked_paths=("known.py", "pipeline.py", *candidate_paths),
                tracked_executable_paths=("tool",),
                check_document=False,
            )
        missing = {
            item["path"]
            for item in report["failures"]
            if item["code"] == "trigger_file_missing_from_baseline"
        }
        self.assertEqual(set(candidate_paths), missing)

    def test_coordinated_manifest_line_raise_fails_historical_ratchet(self) -> None:
        prior = _baseline(_file_entry("known.py", 401))
        current = _baseline(_file_entry("known.py", 450))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_common(root, known_lines=450)
            report = verify_baseline(
                current,
                root,
                tracked_paths=("known.py", "pipeline.py"),
                ratchet_baselines=(prior,),
                check_document=False,
            )
        self.assertIn(
            {
                "code": "ratchet_physical_line_increase",
                "path": "known.py",
                "historical_minimum": 401,
                "current": 450,
            },
            report["failures"],
        )

    def test_new_grandfathered_path_fails_historical_ratchet(self) -> None:
        prior = _baseline(_file_entry("known.py", 401))
        current = _baseline(_file_entry("new.py", 601), _file_entry("known.py", 401))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_common(root)
            (root / "new.py").write_text("pass\n" * 601, encoding="utf-8", newline="\n")
            report = verify_baseline(
                current,
                root,
                tracked_paths=("known.py", "new.py", "pipeline.py"),
                ratchet_baselines=(prior,),
                check_document=False,
            )
        self.assertIn(
            {"code": "ratchet_new_trigger_path", "path": "new.py"},
            report["failures"],
        )

    def test_document_match_normalizes_crlf_but_rejects_extra_framing_lines(self) -> None:
        baseline = _baseline()
        rendered = render_document(baseline)
        start = baseline["document"]["start_marker"]
        end = baseline["document"]["end_marker"]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_common(root)
            exact = f"intro\r\n{start}\r\n{rendered.replace(chr(10), chr(13) + chr(10))}\r\n{end}\r\n"
            (root / "code-map.md").write_bytes(exact.encode("utf-8"))
            compatible = verify_baseline(
                baseline,
                root,
                tracked_paths=("known.py", "pipeline.py"),
            )
            extra = exact.replace(f"{start}\r\n", f"{start}\r\n\r\n", 1)
            (root / "code-map.md").write_bytes(extra.encode("utf-8"))
            incompatible = verify_baseline(
                baseline,
                root,
                tracked_paths=("known.py", "pipeline.py"),
            )
        self.assertEqual("compatible", compatible["status"])
        self.assertIn("generated_document_drift", {item["code"] for item in incompatible["failures"]})


if __name__ == "__main__":
    unittest.main()
