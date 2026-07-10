from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


MAX_BUNDLE_FILES = 10_000
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_READ_BYTES = 64 * 1024 * 1024
TERMINAL_STATUS_ALIASES = {
    "done": "completed",
    "complete": "completed",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "partial": "partial_completed",
    "partial_completed": "partial_completed",
}
SECRET_PATTERNS = {
    "openai_api_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "pullwise_worker_token": re.compile(r"\bpww_[A-Za-z0-9_-]{24,}\b"),
    "github_token": re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
}


def normalized_name(value: object) -> str:
    text = str(value or "").replace("\\", "/").lstrip("/")
    parts = PurePosixPath(text).parts
    if not text or any(part in {"", ".", ".."} for part in parts):
        return ""
    return PurePosixPath(*parts).as_posix()


class BundleFiles:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._zip: zipfile.ZipFile | None = None
        self._zip_names: dict[str, zipfile.ZipInfo] = {}
        self._directory_names: dict[str, Path] = {}
        if path.is_file():
            if not zipfile.is_zipfile(path):
                raise ValueError(f"bundle is not a ZIP archive: {path}")
            self._zip = zipfile.ZipFile(path)
            infos = [info for info in self._zip.infolist() if not info.is_dir()]
            if len(infos) > MAX_BUNDLE_FILES:
                raise ValueError(f"bundle has too many files: {len(infos)}")
            total_size = sum(max(0, int(info.file_size)) for info in infos)
            if total_size > MAX_BUNDLE_BYTES:
                raise ValueError(f"bundle is too large after decompression: {total_size}")
            for info in infos:
                name = normalized_name(info.filename)
                if name:
                    self._zip_names[name] = info
        elif path.is_dir():
            for candidate in path.rglob("*"):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                name = normalized_name(candidate.relative_to(path).as_posix())
                if name:
                    self._directory_names[name] = candidate
                if len(self._directory_names) > MAX_BUNDLE_FILES:
                    raise ValueError(f"bundle has too many files: {len(self._directory_names)}")
        else:
            raise ValueError(f"bundle path does not exist: {path}")

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()

    @property
    def names(self) -> list[str]:
        source = self._zip_names if self._zip is not None else self._directory_names
        return sorted(source)

    def read_bytes(self, name: str) -> bytes:
        safe_name = normalized_name(name)
        if not safe_name:
            raise KeyError(name)
        if self._zip is not None:
            info = self._zip_names[safe_name]
            if info.file_size > MAX_READ_BYTES:
                raise ValueError(f"bundle member is too large to inspect: {safe_name}")
            return self._zip.read(info)
        path = self._directory_names[safe_name]
        if path.stat().st_size > MAX_READ_BYTES:
            raise ValueError(f"bundle member is too large to inspect: {safe_name}")
        return path.read_bytes()

    def find(self, *suffixes: str) -> str:
        safe_suffixes = [normalized_name(suffix) for suffix in suffixes if normalized_name(suffix)]
        for suffix in safe_suffixes:
            if suffix in self.names:
                return suffix
        candidates = [
            name
            for name in self.names
            if any(name == suffix or name.endswith("/" + suffix) for suffix in safe_suffixes)
        ]
        return min(candidates, key=lambda item: (item.count("/"), len(item), item)) if candidates else ""


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    path: str = ""
    details: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.path:
            result["path"] = self.path
        if self.details:
            result["details"] = self.details
        return result


def integer(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def status_name(value: object) -> str:
    text = str(value or "").strip().lower()
    return TERMINAL_STATUS_ALIASES.get(text, text)


def list_value(payload: Any, *keys: str) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def record_id(record: Any, fallback: str = "") -> str:
    if not isinstance(record, dict):
        return fallback
    return str(
        record.get("test_id")
        or record.get("testId")
        or record.get("target_id")
        or record.get("targetId")
        or record.get("id")
        or fallback
    ).strip()


def related_test_ids(record: Any) -> list[str]:
    if not isinstance(record, dict):
        return []
    result: list[str] = []
    for key in (
        "test_ids",
        "testIds",
        "target_ids",
        "targetIds",
        "target_test_ids",
        "targetTestIds",
        "related_test_ids",
        "relatedTestIds",
        "targets",
    ):
        raw = record.get(key)
        values = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
        for value in values:
            text = str(value or "").strip()
            if text and text not in result:
                result.append(text)
    return result


def logical_test_ids(records: Any, prefix: str, *, use_related: bool = True) -> set[str]:
    result: set[str] = set()
    if not isinstance(records, list):
        return result
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        related = related_test_ids(record) if use_related else []
        if related:
            result.update(related)
        else:
            test_id = record_id(record, f"{prefix}-{index + 1:03d}")
            if test_id:
                result.add(test_id)
    return result


def executable_generated_tests(records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    objects = [record for record in records if isinstance(record, dict)]
    executable = [
        record
        for record in objects
        if str(record.get("path_kind") or record.get("pathKind") or "").strip().lower()
        not in {"run_artifact_source_copy", "artifact_source_copy"}
    ]
    return executable or objects


def finding_locations(finding: Any) -> list[dict[str, Any]]:
    if not isinstance(finding, dict):
        return []
    raw_locations: list[Any] = []
    for key in ("locations", "affected_locations", "affectedLocations"):
        value = finding.get(key)
        if isinstance(value, list):
            raw_locations.extend(value)
    if isinstance(finding.get("location"), dict):
        raw_locations.append(finding["location"])
    if not raw_locations and str(finding.get("path") or finding.get("file") or "").strip():
        raw_locations.append(finding)
    result: list[dict[str, Any]] = []
    for raw in raw_locations:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or raw.get("file") or raw.get("file_path") or "").strip().replace("\\", "/")
        line_range = raw.get("line_range") or raw.get("lineRange")
        start = raw.get("start_line") or raw.get("startLine") or raw.get("line_start") or raw.get("lineStart") or raw.get("line")
        end = raw.get("end_line") or raw.get("endLine") or raw.get("line_end") or raw.get("lineEnd")
        if isinstance(line_range, list) and line_range:
            start = start or line_range[0]
            end = end or (line_range[1] if len(line_range) > 1 else line_range[0])
        elif isinstance(line_range, dict):
            start = start or line_range.get("start") or line_range.get("start_line")
            end = end or line_range.get("end") or line_range.get("end_line")
        start_line = integer(start)
        end_line = integer(end, start_line)
        if path:
            result.append({"path": path, "start_line": start_line, "end_line": end_line})
    return result


class BundleAudit:
    def __init__(self, files: BundleFiles) -> None:
        self.files = files
        self.issues: list[Issue] = []
        self.facts: dict[str, Any] = {}
        self.debug_name = files.find("worker/debug-summary.json", "debug-summary.json")
        debug_parent = PurePosixPath(self.debug_name).parent.as_posix() if self.debug_name else ""
        self.worker_prefix = "" if debug_parent == "." else debug_parent
        self.run_prefix = f"{self.worker_prefix}/run".strip("/")

    def issue(self, severity: str, code: str, message: str, path: str = "", **details: Any) -> None:
        self.issues.append(Issue(severity, code, message, path, details or None))

    def run_name(self, relative: str) -> str:
        direct = f"{self.run_prefix}/{relative}".strip("/")
        return self.files.find(direct, f"run/{relative}", relative)

    def json_at(self, name: str, *, required: bool = False) -> Any:
        if not name:
            if required:
                self.issue("error", "required_artifact_missing", "Required JSON artifact is missing")
            return {}
        try:
            return json.loads(self.files.read_bytes(name).decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.issue("error", "invalid_json", f"JSON artifact cannot be parsed: {type(exc).__name__}", name)
            return {}

    def text_at(self, name: str) -> str:
        if not name:
            return ""
        try:
            return self.files.read_bytes(name).decode("utf-8", errors="replace")
        except (OSError, ValueError, KeyError):
            return ""

    def audit(self) -> dict[str, Any]:
        debug = self.json_at(self.debug_name, required=True)
        progress_name = self.run_name("progress.json")
        progress = self.json_at(progress_name)
        self.audit_status(debug, progress, progress_name)
        inventory = self.json_at(self.run_name("inventory.json"))
        coverage = self.json_at(self.run_name("coverage.json"))
        self.audit_coverage(inventory, coverage)
        self.audit_report(inventory)
        intent_counts = self.audit_intent()
        self.audit_progress(inventory, progress, progress_name, intent_counts)
        self.audit_artifacts()
        self.audit_runtime()
        self.audit_events()
        self.audit_secrets()
        counts = {
            severity: sum(1 for issue in self.issues if issue.severity == severity)
            for severity in ("error", "warning", "info")
        }
        return {
            "schema_version": "pullwise-debug-bundle-audit/v1",
            "bundle": str(self.files.path),
            "summary": {
                "status": "fail" if counts["error"] else "warn" if counts["warning"] else "pass",
                "errors": counts["error"],
                "warnings": counts["warning"],
                "info": counts["info"],
            },
            "facts": self.facts,
            "issues": [issue.payload() for issue in self.issues],
        }

    def audit_status(self, debug: Any, progress: Any, progress_name: str) -> None:
        debug_status = status_name(debug.get("status")) if isinstance(debug, dict) else ""
        progress_status = status_name(progress.get("status")) if isinstance(progress, dict) else ""
        terminal_status = progress_status or debug_status or "unknown"
        self.facts["run_id"] = str((debug if isinstance(debug, dict) else {}).get("run_id") or "")
        self.facts["terminal_status"] = terminal_status
        if debug_status and progress_status and debug_status != progress_status:
            self.issue(
                "error",
                "debug_status_mismatch",
                f"debug-summary reports {debug_status}, but progress reports {progress_status}",
                self.debug_name,
                progress_path=progress_name,
            )
        debug_run_id = str(debug.get("run_id") or "").strip() if isinstance(debug, dict) else ""
        progress_run_id = str(progress.get("run_id") or "").strip() if isinstance(progress, dict) else ""
        if debug_run_id and progress_run_id and debug_run_id != progress_run_id:
            self.issue("error", "run_id_mismatch", "debug-summary and progress run_id values differ", progress_name)
        qa_name = self.run_name("qa.json")
        qa = self.json_at(qa_name)
        qa_status = str(qa.get("status") or "").strip().lower() if isinstance(qa, dict) else ""
        self.facts["qa_status"] = qa_status or "missing"
        if terminal_status == "completed" and qa_status in {"fail", "failed", "error"}:
            self.issue("error", "completed_with_failed_qa", "Completed run has a failed QA gate", qa_name)

    def audit_coverage(self, inventory: Any, coverage: Any) -> None:
        files = list_value(inventory, "files")
        source_paths = {
            str(item.get("path") or "").strip()
            for item in files
            if isinstance(item, dict) and item.get("is_source_like") is True
        }
        total = integer((coverage if isinstance(coverage, dict) else {}).get("source_like_files_total"), len(source_paths))
        partition_keys = (
            "deep_reviewed_files",
            "standard_reviewed_files",
            "light_reviewed_files",
            "inventory_only_files",
            "skipped_files",
        )
        partition_total = sum(integer(coverage.get(key)) for key in partition_keys) if isinstance(coverage, dict) else 0
        self.facts["source_like_files"] = total
        if total and partition_total != total:
            self.issue(
                "error",
                "coverage_partition_mismatch",
                f"Coverage partition totals {partition_total}, expected {total}",
                self.run_name("coverage.json"),
            )
        if source_paths and total != len(source_paths):
            self.issue(
                "warning",
                "coverage_inventory_mismatch",
                f"Coverage reports {total} source-like files, inventory contains {len(source_paths)}",
                self.run_name("coverage.json"),
            )

    def audit_report(self, inventory: Any) -> None:
        report_name = self.run_name("report.agent.json")
        report = self.json_at(report_name)
        findings = list_value(report, "findings", "main_findings", "mainFindings")
        self.facts["findings"] = len(findings)
        inventory_paths = {
            str(item.get("path") or "").strip().replace("\\", "/")
            for item in list_value(inventory, "files")
            if isinstance(item, dict)
        }
        location_count = 0
        for index, finding in enumerate(findings):
            finding_id = str((finding if isinstance(finding, dict) else {}).get("id") or f"finding-{index + 1}")
            locations = finding_locations(finding)
            if not locations:
                self.issue("error", "finding_location_missing", f"Finding {finding_id} has no usable source location", report_name)
                continue
            location_count += len(locations)
            for location in locations:
                if location["start_line"] <= 0 or location["end_line"] <= 0:
                    self.issue(
                        "error",
                        "report_location_invalid",
                        f"Finding {finding_id} has a non-positive line range",
                        report_name,
                        location=location,
                    )
                elif location["end_line"] < location["start_line"]:
                    self.issue(
                        "error",
                        "report_location_reversed",
                        f"Finding {finding_id} has a reversed line range",
                        report_name,
                        location=location,
                    )
                if inventory_paths and location["path"] not in inventory_paths:
                    self.issue(
                        "warning",
                        "report_path_not_in_inventory",
                        f"Finding {finding_id} path is absent from inventory: {location['path']}",
                        report_name,
                    )
        verification_name = self.run_name("location-verification.json")
        verification = self.json_at(verification_name)
        verification_records = list_value(
            verification,
            "locations",
            "results",
            "verified_locations",
            "verifiedLocations",
        )
        summary = (
            verification.get("summary")
            if isinstance(verification, dict) and isinstance(verification.get("summary"), dict)
            else {}
        )
        verified_total = max(
            len(verification_records),
            integer(summary.get("locations_total")),
            integer(summary.get("total_locations")),
            integer(summary.get("checked_locations")),
        )
        if findings and verified_total == 0:
            self.issue(
                "error",
                "location_verification_missing",
                f"{len(findings)} reported finding(s) have no mechanical location verification records",
                verification_name,
            )
        elif location_count and verified_total < location_count:
            self.issue(
                "warning",
                "location_verification_incomplete",
                f"Only {verified_total} of {location_count} report locations were mechanically verified",
                verification_name,
            )

    def audit_intent(self) -> dict[str, int]:
        plan_name = self.run_name("intent/intent-test-plan.json")
        source_name = self.run_name("intent/intent-test-source.json")
        raw_name = self.run_name("intent/intent-test-results.raw.json")
        plan = self.json_at(plan_name)
        source = self.json_at(source_name)
        raw = self.json_at(raw_name)
        targets = list_value(plan, "test_targets", "tests")
        all_generated = [
            record
            for record in list_value(source, "generated_tests", "tests")
            if isinstance(record, dict)
        ]
        generated = executable_generated_tests(all_generated)
        runs = list_value(raw, "test_runs", "results")
        planned_ids = logical_test_ids(targets, "ITP", use_related=False)
        written_ids = logical_test_ids(generated, "ITV")
        generated_id_map = {
            record_id(record): set(related_test_ids(record) or [record_id(record)])
            for record in all_generated
            if record_id(record)
        }
        run_ids: set[str] = set()
        for index, run in enumerate(runs):
            if not isinstance(run, dict):
                continue
            related = related_test_ids(run)
            raw_id = record_id(run, f"ITR-{index + 1:03d}")
            if related:
                run_ids.update(related)
            elif raw_id in generated_id_map:
                run_ids.update(generated_id_map[raw_id])
            elif raw_id:
                run_ids.add(raw_id)
        uncovered = planned_ids - written_ids
        if uncovered:
            self.issue(
                "warning",
                "intent_targets_uncovered",
                f"{len(uncovered)} planned intent target(s) are not linked from generated tests",
                source_name,
                test_ids=sorted(uncovered),
            )
        no_command_plan_ids = {
            record_id(run)
            for run in runs
            if isinstance(run, dict)
            and "no generated test command" in str(run.get("skip_reason") or "").lower()
            and record_id(run) in planned_ids
        }
        generated_run_ids = {record_id(record) for record in generated if record_id(record)}
        observed_raw_ids = {record_id(record) for record in runs if record_id(record)}
        if no_command_plan_ids and generated_run_ids.intersection(observed_raw_ids):
            self.issue(
                "error",
                "intent_duplicate_plan_execution",
                "Plan target IDs were emitted as commandless runs in addition to their generated test record",
                raw_name,
                test_ids=sorted(no_command_plan_ids),
            )
        raw_by_id = {
            record_id(record): record
            for record in runs
            if isinstance(record, dict) and record_id(record)
        }
        for generated_record in generated:
            framework = str(
                generated_record.get("test_framework")
                or generated_record.get("testFramework")
                or generated_record.get("framework")
                or ""
            ).strip().lower()
            if framework not in {"unittest", "python-unittest"}:
                continue
            raw_record = raw_by_id.get(record_id(generated_record), {})
            if "pytest" in json.dumps(raw_record, ensure_ascii=False).lower():
                self.issue(
                    "error",
                    "intent_unittest_routed_to_pytest",
                    f"Generated unittest {record_id(generated_record)} was routed through pytest",
                    raw_name,
                )
        counts = {
            "total": len(planned_ids | written_ids | run_ids),
            "written": len(written_ids),
            "run": len(run_ids),
        }
        self.facts["intent_tests"] = counts
        return counts

    def audit_progress(
        self,
        inventory: Any,
        progress: Any,
        progress_name: str,
        intent_counts: dict[str, int],
    ) -> None:
        if not isinstance(progress, dict):
            return
        counters = progress.get("counters") if isinstance(progress.get("counters"), dict) else {}
        source_paths = {
            str(item.get("path") or "").strip()
            for item in list_value(inventory, "files")
            if isinstance(item, dict) and item.get("is_source_like") is True
        }
        effective_routing_name = self.run_name("effective-risk-routing.json")
        routing = self.json_at(effective_routing_name) if effective_routing_name else self.json_at(self.run_name("risk-routing.json"))
        route_paths = {
            str(route.get("path") or "").strip()
            for route in list_value(routing, "routes")
            if isinstance(route, dict)
        }
        plan = self.json_at(self.run_name("bundle-plan.json"))
        bundles = list_value(plan, "bundles")
        bundle_ids = {
            str(bundle.get("bundle_id") or bundle.get("id") or "").strip()
            for bundle in bundles
            if isinstance(bundle, dict)
        }
        packed_names = {
            PurePosixPath(name).stem
            for name in self.files.names
            if self.run_prefix
            and name.startswith(self.run_prefix + "/bundles/")
            and name.endswith(".md")
        }
        validation_input = self.json_at(self.run_name("validation-input.json"))
        validation_output = self.json_at(self.run_name("validated-findings.json"))
        candidates = list_value(
            validation_input,
            "candidates",
            "candidate_findings",
            "findings",
            "clusters",
        )
        validated = list_value(validation_output, "validated_findings", "findings", "results")
        expected = {
            "source_like_files_total": len(source_paths),
            "source_like_files_classified": len(source_paths.intersection(route_paths)) if route_paths else 0,
            "bundles_total": len(bundles),
            "bundles_packed": len(bundle_ids.intersection(packed_names)),
            "intent_tests_total": intent_counts["total"],
            "intent_tests_written": intent_counts["written"],
            "intent_tests_run": intent_counts["run"],
            "validator_candidates_total": max(len(candidates), len(validated)),
            "validator_candidates_completed": len(validated),
        }
        mismatches = {
            key: {"recorded": integer(counters.get(key)), "expected": value}
            for key, value in expected.items()
            if value and integer(counters.get(key)) != value
        }
        if mismatches:
            self.issue(
                "error",
                "progress_counter_mismatch",
                f"{len(mismatches)} progress counter(s) disagree with persisted artifacts",
                progress_name,
                counters=mismatches,
            )

    def audit_artifacts(self) -> None:
        manifest_name = self.files.find(
            f"{self.worker_prefix}/artifacts/artifact-manifest.json".strip("/"),
            "artifacts/artifact-manifest.json",
            self.run_name("artifact-manifest.json"),
        )
        if not manifest_name:
            return
        manifest = self.json_at(manifest_name)
        items = list_value(manifest, "items", "artifacts")
        manifest_parent = PurePosixPath(manifest_name).parent.as_posix()
        for item in items:
            if not isinstance(item, dict):
                continue
            name = normalized_name(item.get("name") or item.get("path"))
            if not name or name == "debug-bundle.zip":
                continue
            candidate = self.files.find(
                f"{manifest_parent}/{name}",
                f"{self.run_prefix}/{name}",
                name,
            )
            if not candidate:
                if item.get("required") is True:
                    self.issue(
                        "error",
                        "required_artifact_missing",
                        f"Required manifest artifact is missing: {name}",
                        manifest_name,
                    )
                continue
            try:
                content = self.files.read_bytes(candidate)
            except (OSError, ValueError, KeyError) as exc:
                self.issue(
                    "error",
                    "artifact_unreadable",
                    f"Artifact cannot be read: {type(exc).__name__}",
                    candidate,
                )
                continue
            expected_size = integer(item.get("size_bytes") or item.get("sizeBytes"), -1)
            expected_hash = str(item.get("sha256") or "").strip().lower()
            if expected_size >= 0 and expected_size != len(content):
                self.issue(
                    "error",
                    "artifact_size_mismatch",
                    f"Artifact size mismatch for {name}",
                    candidate,
                )
            if expected_hash and hashlib.sha256(content).hexdigest() != expected_hash:
                self.issue(
                    "error",
                    "artifact_hash_mismatch",
                    f"Artifact hash mismatch for {name}",
                    candidate,
                )

    def audit_runtime(self) -> None:
        runtime_name = self.run_name("codex-runtime.json")
        runtime = self.json_at(runtime_name)
        if isinstance(runtime, dict) and runtime:
            self.facts["codex_runtime"] = runtime
        log_name = self.run_name("worker.log.jsonl")
        log_text = self.text_at(log_name).lower()
        if "requires a newer version of codex" in log_text:
            self.issue(
                "error",
                "codex_runtime_too_old",
                "The configured Codex runtime rejected the requested model as too old",
                log_name,
            )
        elif not runtime_name:
            self.issue(
                "warning",
                "codex_runtime_metadata_missing",
                "Bundle does not record Python SDK, bundled CLI, and configured CLI versions",
            )

    def audit_events(self) -> None:
        progress_log = self.run_name("progress.log.jsonl")
        text = self.text_at(progress_log)
        if not text:
            return
        sequences: list[int] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                self.issue(
                    "error",
                    "invalid_jsonl",
                    f"Invalid JSONL at line {line_number}",
                    progress_log,
                )
                continue
            if isinstance(payload, dict) and isinstance(payload.get("sequence"), int):
                sequences.append(payload["sequence"])
        if any(next_value <= value for value, next_value in zip(sequences, sequences[1:])):
            self.issue(
                "error",
                "event_sequence_not_monotonic",
                "Progress event sequence is not strictly increasing",
                progress_log,
            )

    def audit_secrets(self) -> None:
        text_suffixes = (".json", ".jsonl", ".log", ".txt", ".md", ".toml", ".env")
        for name in self.files.names:
            if not name.lower().endswith(text_suffixes):
                continue
            text = self.text_at(name)
            if not text:
                continue
            for secret_type, pattern in SECRET_PATTERNS.items():
                if pattern.search(text):
                    self.issue(
                        "error",
                        "possible_secret_exposure",
                        f"Possible {secret_type} value is present in the debug bundle",
                        name,
                    )


def audit_bundle(path: str | Path) -> dict[str, Any]:
    files = BundleFiles(Path(path))
    try:
        return BundleAudit(files).audit()
    finally:
        files.close()


def discover_bundles(paths: Iterable[str | Path]) -> list[Path]:
    discovered: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        candidates: list[Path] = []
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            if (path / "debug-summary.json").is_file() or (path / "worker" / "debug-summary.json").is_file():
                candidates = [path]
            else:
                candidates.extend(sorted(path.rglob("*.zip")))
                for summary in sorted(path.rglob("debug-summary.json")):
                    root = summary.parent.parent if summary.parent.name == "worker" else summary.parent
                    candidates.append(root)
        else:
            candidates = [path]
        for candidate in candidates:
            key = str(candidate.resolve(strict=False)).lower()
            if key not in seen:
                seen.add(key)
                discovered.append(candidate)
    return discovered


def audit_inputs(paths: Iterable[str | Path]) -> dict[str, Any]:
    bundles = []
    for path in discover_bundles(paths):
        try:
            bundles.append(audit_bundle(path))
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            bundles.append(
                {
                    "schema_version": "pullwise-debug-bundle-audit/v1",
                    "bundle": str(path),
                    "summary": {"status": "fail", "errors": 1, "warnings": 0, "info": 0},
                    "facts": {},
                    "issues": [
                        {
                            "severity": "error",
                            "code": "bundle_unreadable",
                            "message": str(exc),
                        }
                    ],
                }
            )
    errors = sum(integer(bundle.get("summary", {}).get("errors")) for bundle in bundles)
    warnings = sum(integer(bundle.get("summary", {}).get("warnings")) for bundle in bundles)
    return {
        "schema_version": "pullwise-debug-bundle-audit-collection/v1",
        "summary": {
            "bundles": len(bundles),
            "errors": errors,
            "warnings": warnings,
            "status": "fail" if errors else "warn" if warnings else "pass",
        },
        "bundles": bundles,
    }


def markdown_report(collection: dict[str, Any]) -> str:
    summary = collection.get("summary") if isinstance(collection.get("summary"), dict) else {}
    lines = [
        "# Pullwise debug bundle audit",
        "",
        f"- Bundles: {integer(summary.get('bundles'))}",
        f"- Errors: {integer(summary.get('errors'))}",
        f"- Warnings: {integer(summary.get('warnings'))}",
        "",
    ]
    for bundle in collection.get("bundles", []):
        if not isinstance(bundle, dict):
            continue
        bundle_summary = bundle.get("summary") if isinstance(bundle.get("summary"), dict) else {}
        lines.extend(
            [
                f"## {bundle.get('bundle')}",
                "",
                f"Status: {bundle_summary.get('status', 'unknown')}",
                "",
            ]
        )
        issues = bundle.get("issues") if isinstance(bundle.get("issues"), list) else []
        if not issues:
            lines.extend(["No issues found.", ""])
            continue
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            path_text = f" ({issue.get('path')})" if issue.get("path") else ""
            lines.append(
                f"- [{str(issue.get('severity') or 'info').upper()}] "
                f"{issue.get('code')}: {issue.get('message')}{path_text}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit Pullwise live debug bundles without extracting ZIP files."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Bundle ZIP, extracted bundle directory, or directory containing bundles.",
    )
    parser.add_argument("--json-out", help="Write the collection report as JSON.")
    parser.add_argument("--markdown-out", help="Write a human-readable Markdown report.")
    parser.add_argument(
        "--fail-on",
        choices=("error", "warning", "never"),
        default="error",
        help="Choose which findings produce a non-zero exit status.",
    )
    args = parser.parse_args(argv)
    collection = audit_inputs(args.paths)
    rendered_json = json.dumps(collection, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        Path(args.json_out).write_text(rendered_json, encoding="utf-8")
    else:
        sys.stdout.write(rendered_json)
    if args.markdown_out:
        Path(args.markdown_out).write_text(markdown_report(collection), encoding="utf-8")
    summary = collection["summary"]
    if args.fail_on == "error" and summary["errors"]:
        return 1
    if args.fail_on == "warning" and (summary["errors"] or summary["warnings"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
