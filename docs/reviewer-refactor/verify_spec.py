from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


HERE = Path(__file__).resolve().parent


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("reviewer_spec_model", "_spec_verifier_model.py")
json_module = _load("reviewer_spec_json", "_spec_verifier_json.py")
_load("reviewer_spec_cards", "_spec_verifier_cards.py")
core_module = _load("reviewer_spec_core", "_spec_verifier_core.py")
selftest_module = _load("reviewer_spec_selftest", "_spec_verifier_selftest.py")


def _emit(value: dict[str, object]) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    root = Path(args.repo_root)
    try:
        if args.self_test:
            result = selftest_module.self_test(root, core_module.verify)
        else:
            result = core_module.verify(root)
    except json_module.SpecError as exc:
        _emit(
            {
                "schema_id": "reviewer-refactor-spec-verification/v1",
                "status": "FAIL",
                "reason_code": exc.code,
                "detail": exc.detail,
            }
        )
        return 1
    except Exception as exc:
        _emit(
            {
                "schema_id": "reviewer-refactor-spec-verification/v1",
                "status": "INDETERMINATE",
                "reason_code": "verifier.unhandled",
                "detail": type(exc).__name__,
            }
        )
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
