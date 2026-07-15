#!/usr/bin/env python3
"""Run fixture-backed checks against Pullwise's real semantic output validators."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pullwise_worker.review_worker_v1 import validate_phase_outputs, validate_reviewer_outputs


DEFAULT_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "output_contracts"


def _write_case_files(run_dir: Path, files: object) -> None:
    if not isinstance(files, dict) or not files:
        raise ValueError("case files must be a non-empty object")
    for relative_name, payload in files.items():
        relative_path = Path(str(relative_name))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"case file path must stay relative: {relative_name}")
        destination = run_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, str):
            destination.write_text(payload, encoding="utf-8")
        else:
            destination.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )


def _lookup(payload: object, dotted_path: str) -> object:
    current = payload
    for part in dotted_path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(dotted_path)
    return current


def _assert_case_outputs(run_dir: Path, assertions: object) -> None:
    if assertions is None:
        return
    if not isinstance(assertions, dict):
        raise ValueError("case assertions must be an object")
    for relative_name, expected_values in assertions.items():
        path = run_dir / str(relative_name)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(expected_values, dict):
            raise ValueError(f"assertions for {relative_name} must be an object")
        for dotted_path, expected in expected_values.items():
            actual = _lookup(payload, str(dotted_path))
            if actual != expected:
                raise AssertionError(
                    f"{relative_name}:{dotted_path} expected {expected!r}, got {actual!r}"
                )


def _validate_case(run_dir: Path, phase: str) -> None:
    if phase == "reviewer_json_validation":
        validate_reviewer_outputs(run_dir)
        return
    validate_phase_outputs(run_dir, phase)


def run_case(case: object) -> str | None:
    if not isinstance(case, dict):
        return "case must be an object"
    case_id = str(case.get("case_id") or "unnamed-case")
    phase = str(case.get("phase") or "").strip()
    expectation = str(case.get("expect") or "valid").strip().lower()
    if not phase:
        return f"{case_id}: phase is required"
    if expectation not in {"valid", "invalid"}:
        return f"{case_id}: expect must be valid or invalid"

    with tempfile.TemporaryDirectory(prefix="pullwise-output-contract-") as tmp_dir:
        run_dir = Path(tmp_dir) / ".codex-review" / "runs" / "fixture"
        try:
            _write_case_files(run_dir, case.get("files"))
            _validate_case(run_dir, phase)
        except Exception as exc:
            if expectation == "invalid":
                expected_error = str(case.get("expected_error") or "")
                if expected_error and expected_error not in str(exc):
                    return (
                        f"{case_id}: expected error containing {expected_error!r}, "
                        f"got {str(exc)!r}"
                    )
                return None
            return f"{case_id}: expected valid output, got {type(exc).__name__}: {exc}"

        if expectation == "invalid":
            return f"{case_id}: expected invalid output, but validation passed"
        try:
            _assert_case_outputs(run_dir, case.get("assertions"))
        except Exception as exc:
            return f"{case_id}: output assertion failed: {exc}"
    return None


def load_cases(fixture_root: Path) -> list[dict[str, Any]]:
    paths = [fixture_root] if fixture_root.is_file() else sorted(fixture_root.glob("*.json"))
    cases: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_cases = payload.get("cases") if isinstance(payload, dict) else None
        if not isinstance(raw_cases, list):
            raise ValueError(f"fixture must contain a cases array: {path}")
        cases.extend(raw_cases)
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check model-output fixture variants with the worker's production validators."
    )
    parser.add_argument(
        "fixture_root",
        nargs="?",
        type=Path,
        default=DEFAULT_FIXTURE_ROOT,
        help="A fixture JSON file or directory of fixture JSON files.",
    )
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.fixture_root)
    except Exception as exc:
        print(f"output contract fixture load failed: {exc}", file=sys.stderr)
        return 2
    if not cases:
        print("output contract fixture corpus is empty", file=sys.stderr)
        return 2

    failures = [failure for case in cases if (failure := run_case(case))]
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        print(
            f"{len(failures)} of {len(cases)} output contract cases failed",
            file=sys.stderr,
        )
        return 1
    print(f"{len(cases)} output contract cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
