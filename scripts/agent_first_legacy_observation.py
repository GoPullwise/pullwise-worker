"""One-read worktree observations for the D27 legacy-absence ratchet."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping

from scripts.agent_first_contract_files import (
    BaselineEnvironmentError,
    read_surface,
    surface_path,
)
from scripts.agent_first_contract_manifest import ManifestError, validate_manifest
from scripts.agent_first_contract_probes import RUNNER_CATALOG
from scripts.agent_first_legacy_inventory import (
    InventoryError,
    reject_duplicate_keys,
    validate_relative_path,
)


SurfaceKey = tuple[str, str]
ReadSurface = Callable[[Path], bytes]
ResolveSurface = Callable[[Path, str], Path | None]


def _canonical_text(raw: bytes, error_kind: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BaselineEnvironmentError(error_kind) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _git_paths(repo_root: Path, *options: str) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", *options, "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaselineEnvironmentError("worktree_catalog_unavailable") from exc
    if result.returncode != 0:
        raise BaselineEnvironmentError("worktree_catalog_unavailable")
    try:
        decoded = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BaselineEnvironmentError("worktree_path_not_utf8") from exc
    return set(decoded.split("\0")) - {""}


def _worktree_paths(repo_root: Path) -> set[str]:
    catalog = _git_paths(
        repo_root, "--cached", "--others", "--exclude-standard"
    )
    deleted = _git_paths(repo_root, "--cached", "--deleted")
    paths = catalog - deleted
    try:
        for relative in paths:
            validate_relative_path(relative, "worktree.path")
    except InventoryError as exc:
        raise BaselineEnvironmentError("worktree_path_unsafe") from exc
    return paths


def _without_excluded_sections(
    text: str, exclusions: list[dict[str, Any]]
) -> str:
    spans: list[tuple[int, int]] = []
    for item in exclusions:
        start_marker = item["start_marker"]
        end_marker = item["end_marker"]
        if (
            text.count(start_marker) != 1
            or text.count(end_marker) != 1
        ):
            raise InventoryError("evidence_exclusion_markers_invalid")
        start = text.index(start_marker)
        end_start = text.index(end_marker)
        if start + len(start_marker) > end_start:
            raise InventoryError("evidence_exclusion_markers_invalid")
        spans.append((start, end_start + len(end_marker)))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise InventoryError("evidence_exclusion_spans_overlap")
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _load_frozen_baseline(
    inventory: dict[str, Any],
    roots: dict[str, Path],
    *,
    read_file: ReadSurface,
    resolve_file: ResolveSurface,
) -> tuple[list[dict[str, Any]], dict[SurfaceKey, bytes]]:
    binding = inventory["frozen_baseline"]
    relative = binding["path"]
    path = resolve_file(roots["worker"], relative)
    if path is None:
        raise InventoryError("frozen_baseline:missing")
    raw = read_file(path)
    text = _canonical_text(raw, "frozen_baseline_not_utf8")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != binding["text_sha256"]:
        raise InventoryError("frozen_baseline:digest_mismatch")
    try:
        manifest = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                InventoryError(f"non_finite_number:{item}")
            ),
        )
        validate_manifest(manifest, runner_catalog=RUNNER_CATALOG)
    except (json.JSONDecodeError, ManifestError, KeyError, TypeError) as exc:
        raise InventoryError("frozen_baseline:invalid") from exc
    if manifest["baseline_id"] != binding["baseline_id"]:
        raise InventoryError("frozen_baseline:id_mismatch")
    by_id = {item["id"]: item for item in manifest["surfaces"]}
    try:
        selected = [by_id[surface_id] for surface_id in binding["surface_ids"]]
    except KeyError as exc:
        raise InventoryError("frozen_baseline:surface_missing") from exc
    return selected, {("worker", relative): raw}


def _searchable_content(
    key: SurfaceKey,
    raw: bytes,
    exclusions_by_path: dict[SurfaceKey, list[dict[str, Any]]],
) -> str | bytes | None:
    exclusions = exclusions_by_path.get(key, [])
    if exclusions and exclusions[0]["start_marker"] is None:
        return None
    if exclusions:
        return _without_excluded_sections(
            _canonical_text(raw, "bounded_evidence_not_utf8"), exclusions
        )
    return raw


def observe_legacy_surfaces(
    inventory: dict[str, Any],
    roots: dict[str, Path],
    signatures: dict[str, str],
    *,
    read_file: ReadSurface = read_surface,
    resolve_file: ResolveSurface = surface_path,
    initial_snapshot: Mapping[SurfaceKey, bytes] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return explicit/baseline reports and ratchet violations from one snapshot."""

    control_snapshot = dict(initial_snapshot or {})
    baseline_surfaces, baseline_snapshot = _load_frozen_baseline(
        inventory,
        roots,
        read_file=read_file,
        resolve_file=resolve_file,
    )
    snapshot: dict[SurfaceKey, bytes | None] = dict(control_snapshot)
    for key, raw in baseline_snapshot.items():
        if key in snapshot and snapshot[key] != raw:
            raise BaselineEnvironmentError("observation_snapshot_conflict")
        snapshot[key] = raw
    worktree_paths = {
        repo_id: _worktree_paths(repo_root)
        for repo_id, repo_root in roots.items()
    }
    explicit_by_path = {
        (item["repo"], item["path"]): item
        for item in inventory["surfaces"]
    }
    baseline_paths = {
        (item["repo"], item["path"]) for item in baseline_surfaces
    }
    exclusions_by_path: dict[SurfaceKey, list[dict[str, Any]]] = {}
    for exclusion in inventory["evidence_exclusions"]:
        exclusions_by_path.setdefault(
            (exclusion["repo"], exclusion["path"]), []
        ).append(exclusion)
    keys = set(explicit_by_path) | baseline_paths | set(exclusions_by_path)
    keys.update(
        (repo_id, relative)
        for repo_id, paths in worktree_paths.items()
        for relative in paths
    )
    for key in sorted(keys):
        if key in snapshot:
            continue
        repo_id, relative = key
        path = resolve_file(roots[repo_id], relative)
        if path is None:
            if relative in worktree_paths[repo_id]:
                raise BaselineEnvironmentError("worktree_path_disappeared")
            snapshot[key] = None  # type: ignore[assignment]
            continue
        snapshot[key] = read_file(path)

    searchable = {
        key: (
            None
            if raw is None
            else _searchable_content(key, raw, exclusions_by_path)
        )
        for key, raw in snapshot.items()
    }
    reports: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []
    registered: set[tuple[str, str, str]] = set()
    for surface in inventory["surfaces"]:
        key = (surface["repo"], surface["path"])
        content = searchable[key]
        occurrences: dict[str, int] = {}
        for signature_id, ceiling in surface[
            "signature_occurrence_ceilings"
        ].items():
            registered.add((*key, signature_id))
            literal: str | bytes = signatures[signature_id]
            if isinstance(content, bytes):
                literal = literal.encode("utf-8")
            actual = 0 if content is None else content.count(literal)
            occurrences[signature_id] = actual
            if actual > ceiling:
                unexpected.append(
                    {
                        "kind": "occurrence_ceiling_exceeded",
                        "repo": key[0],
                        "path": key[1],
                        "signature_id": signature_id,
                        "allowed_occurrences": ceiling,
                        "actual_occurrences": actual,
                    }
                )
        matched = [
            signature_id
            for signature_id, count in occurrences.items()
            if count
        ]
        reports.append(
            {
                "id": surface["id"],
                "repo": key[0],
                "path": key[1],
                "source": "high_signal_inventory",
                "status": "present" if matched else "absent",
                "matched_signature_ids": matched,
                "signature_occurrences": occurrences,
            }
        )

    for surface in baseline_surfaces:
        key = (surface["repo"], surface["path"])
        raw = snapshot[key]
        text = (
            ""
            if raw is None
            else _canonical_text(raw, "frozen_surface_not_utf8")
        )
        matched_anchors = [
            anchor for anchor in surface["anchors"] if anchor in text
        ]
        reports.append(
            {
                "id": surface["id"],
                "repo": key[0],
                "path": key[1],
                "source": "frozen_baseline",
                "status": "present" if matched_anchors else "absent",
                "matched_anchor_count": len(matched_anchors),
            }
        )

    for repo_id, paths in worktree_paths.items():
        for relative in sorted(paths):
            key = (repo_id, relative)
            content = searchable[key]
            if content is None:
                continue
            for signature_id, literal_text in signatures.items():
                literal: str | bytes = literal_text
                if isinstance(content, bytes):
                    literal = literal.encode("utf-8")
                if (
                    content.count(literal)
                    and (repo_id, relative, signature_id) not in registered
                ):
                    unexpected.append(
                        {
                            "repo": repo_id,
                            "path": relative,
                            "signature_id": signature_id,
                        }
                    )

    if any(
        _worktree_paths(roots[repo_id]) != initial_paths
        for repo_id, initial_paths in worktree_paths.items()
    ):
        raise BaselineEnvironmentError("worktree_catalog_changed")
    for (repo_id, relative), initial_raw in control_snapshot.items():
        path = resolve_file(roots[repo_id], relative)
        if path is None or read_file(path) != initial_raw:
            raise BaselineEnvironmentError("control_surface_changed")

    report_ids = [item["id"] for item in reports]
    if len(report_ids) != len(set(report_ids)):
        raise InventoryError("legacy_surfaces:duplicate_id")
    reports.sort(key=lambda item: item["id"])
    unexpected.sort(
        key=lambda item: (
            item["repo"],
            item["path"],
            item["signature_id"],
            item.get("kind", ""),
        )
    )
    return reports, unexpected
