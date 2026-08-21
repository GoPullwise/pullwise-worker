#!/usr/bin/env python3
"""Verify R3Q-01 identity without qualifying or allowlisting the runtime."""

from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
import zipfile


EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_OPERATIONAL = 2
EXPECTED_LOCK_DIGEST = "sha256:48e6f0cedbd54f686008b83298fdc81c470293845b2304137535483f481b1399"
ENVIRONMENT_POLICY = {
    "schema_id": "pullwise-codex-environment-policy/v1",
    "inherit_ambient": False,
    "variables": {
        "CODEX_HOME": "instance_scoped",
        "HOME": "instance_scoped",
        "PATH": "fixed_system_minimal",
        "TMPDIR": "attempt_scoped",
    },
}
SANDBOX_POLICY = {
    "schema_id": "pullwise-codex-sandbox-policy/v1",
    "approval_policy": "deny_all",
    "filesystem_modes": ["read_only", "workspace_write"],
    "model_network": False,
    "process_boundary": "supervised_instance_local_cli",
    "shell_tool": False,
}
EXPECTED_ENVIRONMENT_POLICY_DIGEST = "sha256:7af9b7d20b74cd20423e5b63cc932f3f70de6d9426ff273e0503c5f96cc27bf7"
EXPECTED_SANDBOX_POLICY_DIGEST = "sha256:eccc19a3115c7b35f267592fc55642ecd307b23c84c869da354f01ddf158e85a"
EXPECTED_LOCK = {
    "schema_id": "pullwise-codex-runtime-lock/v1",
    "os": "ubuntu-22.04-x86_64",
    "python_version": "3.10.12",
    "sdk": {
        "name": "openai-codex",
        "version": "0.1.0b3",
        "wheel_sha256": (
            "sha256:8d1f9d346667aeecb435c6a45d0edb3f016187276ec452cf8094d813896276c4"
        ),
    },
    "cli_package": {
        "name": "openai-codex-cli-bin",
        "version": "0.137.0a4",
        "artifact_sha256": (
            "sha256:6454f838d44c56c1ed07a29b391fa412785e5dd2ffd06db0b62e62478c19bb64"
        ),
    },
    "cli_executable": {
        "instance_relative_path": "runtime/bin/codex",
        "byte_size": 227705152,
        "sha256": "sha256:86d09b51543bccbf63bd1363e98b7f638e87dd55fe96b1cd22382a5d6bf384ad",
        "version_output": "codex-cli 0.137.0-alpha.4",
    },
    "environment_policy_sha256": EXPECTED_ENVIRONMENT_POLICY_DIGEST,
    "sandbox_policy_sha256": EXPECTED_SANDBOX_POLICY_DIGEST,
    "qualification_report_sha256": None,
    "allowlisted_for_reviewer": False,
}


class RuntimeIdentityError(ValueError):
    """The runtime lock or measured installation is malformed or divergent."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeIdentityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def policy_digests() -> tuple[str, str]:
    return canonical_sha256(ENVIRONMENT_POLICY), canonical_sha256(SANDBOX_POLICY)


def load_runtime_lock(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except RuntimeIdentityError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeIdentityError(f"cannot read runtime lock: {error}") from error
    if value != EXPECTED_LOCK:
        raise RuntimeIdentityError("runtime lock does not match the closed measured identity")
    if canonical_sha256(value) != EXPECTED_LOCK_DIGEST:
        raise RuntimeIdentityError("runtime lock canonical digest mismatch")
    environment_digest, sandbox_digest = policy_digests()
    if environment_digest != EXPECTED_ENVIRONMENT_POLICY_DIGEST:
        raise RuntimeIdentityError("environment policy canonical digest mismatch")
    if sandbox_digest != EXPECTED_SANDBOX_POLICY_DIGEST:
        raise RuntimeIdentityError("sandbox policy canonical digest mismatch")
    return value


def _read_wheel_metadata(path: Path) -> tuple[Any, str | None]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
            if len(metadata_names) != 1 or len(wheel_names) != 1:
                raise RuntimeIdentityError("wheel must contain one METADATA and one WHEEL")
            metadata_text = archive.read(metadata_names[0]).decode("utf-8")
            wheel_text = archive.read(wheel_names[0]).decode("utf-8")
    except RuntimeIdentityError:
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as error:
        raise RuntimeIdentityError(f"cannot inspect wheel {path}: {error}") from error
    metadata = Parser().parsestr(metadata_text)
    tag = next(
        (line.removeprefix("Tag: ") for line in wheel_text.splitlines() if line.startswith("Tag: ")),
        None,
    )
    return metadata, tag


def verify_wheel(
    path: Path,
    *,
    name: str,
    version: str,
    digest: str,
    required_dependency: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    wheel = path.resolve(strict=True)
    if not wheel.is_file():
        raise RuntimeIdentityError(f"wheel is not a file: {wheel}")
    actual_digest = file_sha256(wheel)
    if actual_digest != digest:
        raise RuntimeIdentityError(f"wheel digest mismatch: {wheel.name}")
    metadata, actual_tag = _read_wheel_metadata(wheel)
    if metadata["Name"] != name or metadata["Version"] != version:
        raise RuntimeIdentityError(f"wheel name/version mismatch: {wheel.name}")
    requirements = metadata.get_all("Requires-Dist", failobj=[]) or []
    if required_dependency is not None and required_dependency not in requirements:
        raise RuntimeIdentityError(f"wheel does not lock {required_dependency}")
    if tag is not None and actual_tag != tag:
        raise RuntimeIdentityError(f"wheel platform tag mismatch: {wheel.name}")
    return {
        "path": str(wheel),
        "filename": wheel.name,
        "byte_size": wheel.stat().st_size,
        "sha256": actual_digest,
        "name": metadata["Name"],
        "version": metadata["Version"],
        "tag": actual_tag,
    }


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeIdentityError(f"cannot execute {argv[0]}: {error}") from error
    output = completed.stdout.strip() or completed.stderr.strip()
    if completed.returncode != 0:
        raise RuntimeIdentityError(
            f"command failed with {completed.returncode}: {argv[0]}: {output}"
        )
    return output


def _installed_distributions(python: Path) -> dict[str, str]:
    query = """
import importlib.metadata as metadata
import json
sdk = metadata.distribution("openai-codex")
cli = metadata.distribution("openai-codex-cli-bin")
print(json.dumps({
    "sdk_version": sdk.version,
    "cli_version": cli.version,
    "cli_distribution_root": str(cli.locate_file("").resolve()),
    "cli_executable": str(cli.locate_file("codex_cli_bin/bin/codex").resolve()),
}, sort_keys=True))
"""
    output = _run([str(python), "-I", "-c", query])
    try:
        value = json.loads(output, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, RuntimeIdentityError) as error:
        raise RuntimeIdentityError(f"invalid installed-distribution probe: {error}") from error
    if not isinstance(value, dict) or set(value) != {
        "sdk_version",
        "cli_version",
        "cli_distribution_root",
        "cli_executable",
    }:
        raise RuntimeIdentityError("installed-distribution probe returned an open object")
    if any(not isinstance(item, str) or not item for item in value.values()):
        raise RuntimeIdentityError("installed-distribution probe returned invalid values")
    return value


def _inside(root: Path, candidate: Path, label: str) -> Path:
    resolved_root = root.resolve(strict=True)
    resolved_candidate = candidate.resolve(strict=True)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as error:
        raise RuntimeIdentityError(f"{label} escapes its required root") from error
    return resolved_candidate

def validated_executable_path(path: Path) -> Path:
    if not path.is_absolute() or not path.is_file():
        raise RuntimeIdentityError("Python executable must be an existing absolute path")
    return path


def _version_environment(instance_root: Path) -> dict[str, str]:
    return {
        "CODEX_HOME": str(instance_root / "codex-home"),
        "HOME": str(instance_root / "home"),
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(instance_root / "attempt-tmp"),
    }


def verify_install_identity(
    lock_path: Path,
    sdk_wheel: Path,
    cli_wheel: Path,
    python: Path,
    instance_root: Path,
) -> dict[str, Any]:
    lock = load_runtime_lock(lock_path)
    sdk_report = verify_wheel(
        sdk_wheel,
        name=lock["sdk"]["name"],
        version=lock["sdk"]["version"],
        digest=lock["sdk"]["wheel_sha256"],
        required_dependency="openai-codex-cli-bin==0.137.0a4",
        tag="py3-none-any",
    )
    cli_report = verify_wheel(
        cli_wheel,
        name=lock["cli_package"]["name"],
        version=lock["cli_package"]["version"],
        digest=lock["cli_package"]["artifact_sha256"],
        tag="py3-none-manylinux_2_17_x86_64",
    )

    python_path = validated_executable_path(python)
    if _run([str(python_path), "--version"]) != "Python 3.10.12":
        raise RuntimeIdentityError("Python version mismatch")
    installed = _installed_distributions(python_path)
    if installed["sdk_version"] != lock["sdk"]["version"]:
        raise RuntimeIdentityError("installed SDK version mismatch")
    if installed["cli_version"] != lock["cli_package"]["version"]:
        raise RuntimeIdentityError("installed CLI package version mismatch")

    cli_distribution_root = Path(installed["cli_distribution_root"])
    distribution_executable = _inside(
        cli_distribution_root,
        Path(installed["cli_executable"]),
        "installed CLI executable",
    )
    root = instance_root.resolve(strict=True)
    instance_executable = _inside(
        root,
        root / lock["cli_executable"]["instance_relative_path"],
        "instance CLI executable",
    )
    expected_size = lock["cli_executable"]["byte_size"]
    expected_digest = lock["cli_executable"]["sha256"]
    for label, executable in (
        ("installed", distribution_executable),
        ("instance", instance_executable),
    ):
        if executable.stat().st_size != expected_size:
            raise RuntimeIdentityError(f"{label} CLI executable size mismatch")
        if file_sha256(executable) != expected_digest:
            raise RuntimeIdentityError(f"{label} CLI executable digest mismatch")
    version_output = _run(
        [str(instance_executable), "--version"],
        env=_version_environment(root),
    )
    if version_output != lock["cli_executable"]["version_output"]:
        raise RuntimeIdentityError("instance CLI version output mismatch")

    return {
        "schema_id": "pullwise-r3q-install-identity-report/v1",
        "result": "PASS",
        "runtime_state": "UNQUALIFIED",
        "runtime_lock_sha256": EXPECTED_LOCK_DIGEST,
        "qualification_report_sha256": None,
        "allowlisted_for_reviewer": False,
        "environment_policy_sha256": EXPECTED_ENVIRONMENT_POLICY_DIGEST,
        "sandbox_policy_sha256": EXPECTED_SANDBOX_POLICY_DIGEST,
        "python": {"path": str(python_path), "version": "3.10.12"},
        "sdk_wheel": sdk_report,
        "cli_wheel": cli_report,
        "installed_distributions": installed,
        "cli_executable": {
            "adapter_absolute_path": str(instance_executable),
            "instance_relative_path": lock["cli_executable"]["instance_relative_path"],
            "byte_size": expected_size,
            "sha256": expected_digest,
            "version_output": version_output,
            "matches_installed_distribution": True,
        },
        "selection_controls": {
            "ambient_path_selected": False,
            "global_codex_home_used": False,
            "other_worker_instance_used": False,
            "shim_used": False,
            "fallback_used": False,
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=repo_root / "runtime" / "reviewer-runtime-lock.json",
    )
    parser.add_argument("--sdk-wheel", type=Path, required=True)
    parser.add_argument("--cli-wheel", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--instance-root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = verify_install_identity(
            args.lock,
            args.sdk_wheel,
            args.cli_wheel,
            args.python,
            args.instance_root,
        )
    except RuntimeIdentityError as error:
        print(f"mismatch: {error}", file=sys.stderr)
        return EXIT_MISMATCH
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_OPERATIONAL
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        try:
            args.report.write_text(serialized, encoding="utf-8", newline="\n")
        except OSError as error:
            print(f"error: cannot write report: {error}", file=sys.stderr)
            return EXIT_OPERATIONAL
    print(serialized, end="")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
