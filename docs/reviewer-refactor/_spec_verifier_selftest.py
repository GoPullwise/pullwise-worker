from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from reviewer_spec_json import SpecError, fail, load_json, sha256
from reviewer_spec_model import MANIFEST_REL


Verify = Callable[[Path], dict[str, Any]]


def _copy_unit(root: Path, target: Path) -> dict[str, Any]:
    manifest = load_json(root / MANIFEST_REL)
    for entry in manifest["files"]:
        source = root / entry["path"]
        destination = target / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    destination = target / MANIFEST_REL
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / MANIFEST_REL, destination)
    return manifest


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _refresh_manifest(root: Path, relative: str) -> None:
    manifest_path = root / MANIFEST_REL
    manifest = load_json(manifest_path)
    data = (root / relative).read_bytes()
    entry = next(item for item in manifest["files"] if item["path"] == relative)
    entry["size_bytes"] = len(data)
    entry["sha256"] = sha256(data)
    _write_json(manifest_path, manifest)


def _expect_failure(
    root: Path,
    verify: Verify,
    mutation: Callable[[Path], None],
    expected_code: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="reviewer-refactor-spec-") as directory:
        target = Path(directory)
        _copy_unit(root, target)
        mutation(target)
        try:
            verify(target)
        except SpecError as exc:
            if exc.code != expected_code:
                raise
        else:
            fail("self_test.failure_not_detected", expected_code)


def self_test(root: Path, verify: Verify) -> dict[str, Any]:
    root = root.resolve()
    result = verify(root)

    def tamper(target: Path) -> None:
        path = target / "docs/reviewer-refactor/authority-and-readiness.md"
        path.write_bytes(path.read_bytes() + b"\n")

    _expect_failure(root, verify, tamper, "manifest.size_mismatch")

    def violate_schema(target: Path) -> None:
        relative = "docs/reviewer-refactor/execution-cards.json"
        path = target / relative
        value = load_json(path)
        value["cards"][0]["owner_role"] = ""
        _write_json(path, value)
        _refresh_manifest(target, relative)

    _expect_failure(root, verify, violate_schema, "schema.minLength")

    def violate_lifecycle(target: Path) -> None:
        relative = "docs/reviewer-refactor/execution-cards.json"
        path = target / relative
        value = load_json(path)
        value["cards"][0]["execution_state"] = "bound"
        value["cards"][0]["authority_state"] = "READY"
        _write_json(path, value)
        _refresh_manifest(target, relative)

    _expect_failure(root, verify, violate_lifecycle, "cards.bound_commands")
    result["self_test"] = "PASS"
    result["tamper_detection"] = "PASS"
    result["schema_enforcement"] = "PASS"
    result["lifecycle_enforcement"] = "PASS"
    return result
