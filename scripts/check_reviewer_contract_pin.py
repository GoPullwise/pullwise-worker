#!/usr/bin/env python3
"""Verify the vendored Reviewer contract consumer against its Worker-owned pin."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_OPERATIONAL = 2

PIN_NAME = "reviewer-contract-pin.json"
CONSUMER_RELATIVE_PATH = "pullwise_worker/_generated_reviewer_contract.py"
PIN_KEYS = {
    "schema_id",
    "contract_version",
    "canonicalization",
    "manifest_digest",
    "consumer_path",
    "consumer_sha256",
    "source_commit",
    "source_path",
}
EXPECTED_STATIC = {
    "schema_id": "pullwise-reviewer-contract-pin/v1",
    "contract_version": "pullwise-review/v1",
    "canonicalization": "pullwise-canonical-json/v1",
    "manifest_digest": (
        "sha256:71428f4dc199e7cbdbe99b64cbdeff03686cda59eb08e84f22224822f5a8167e"
    ),
    "consumer_path": CONSUMER_RELATIVE_PATH,
    "source_commit": "556c0dc759d91e159513c2cd8232299981c6f811",
    "source_path": "generated/reviewer-contract-python/reviewer_contract.py",
}
CONSUMER_CONSTANTS = {
    "CONTRACT_VERSION": "contract_version",
    "CANONICALIZATION": "canonicalization",
    "MANIFEST_DIGEST": "manifest_digest",
}


class PinError(ValueError):
    """The pin or vendored consumer is missing, malformed, or divergent."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PinError(f"duplicate key in pin: {key}")
        result[key] = value
    return result


def _load_pin(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except PinError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PinError(f"cannot read pin: {error}") from error
    if not isinstance(payload, dict) or set(payload) != PIN_KEYS:
        raise PinError("pin must be a closed object with the required keys")
    if any(not isinstance(value, str) or not value for value in payload.values()):
        raise PinError("every pin value must be a nonempty string")
    return payload


def _consumer_constants(source: bytes) -> dict[str, str]:
    try:
        module = ast.parse(source.decode("utf-8"))
    except (UnicodeError, SyntaxError) as error:
        raise PinError(f"cannot parse vendored consumer: {error}") from error
    constants: dict[str, str] = {}
    for statement in module.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name) or target.id not in CONSUMER_CONSTANTS:
            continue
        if not isinstance(statement.value, ast.Constant) or not isinstance(
            statement.value.value, str
        ):
            raise PinError(f"consumer constant {target.id} is not a string literal")
        constants[target.id] = statement.value.value
    if set(constants) != set(CONSUMER_CONSTANTS):
        raise PinError("vendored consumer is missing required contract constants")
    return constants


def check(repo_root: Path) -> list[str]:
    root = repo_root.resolve(strict=True)
    pin_path = root / PIN_NAME
    consumer_path = root / CONSUMER_RELATIVE_PATH
    if not pin_path.is_file():
        raise PinError(f"missing pin: {PIN_NAME}")
    if not consumer_path.is_file():
        raise PinError(f"missing vendored consumer: {CONSUMER_RELATIVE_PATH}")

    pin = _load_pin(pin_path)
    source = consumer_path.read_bytes()
    constants = _consumer_constants(source)
    problems = [
        f"pin mismatch for {key}"
        for key, expected in EXPECTED_STATIC.items()
        if pin[key] != expected
    ]
    expected_sha256 = "sha256:" + hashlib.sha256(source).hexdigest()
    if pin["consumer_sha256"] != expected_sha256:
        problems.append("vendored consumer bytes do not match consumer_sha256")
    for constant_name, pin_key in CONSUMER_CONSTANTS.items():
        if constants[constant_name] != pin[pin_key]:
            problems.append(f"vendored consumer {constant_name} does not match pin")
    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the Worker-owned Reviewer contract pin."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Worker repository root (default: parent of scripts/)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        problems = check(args.repo_root)
    except (OSError, PinError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_OPERATIONAL
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return EXIT_MISMATCH
    print("ok: reviewer contract pin is pristine")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
