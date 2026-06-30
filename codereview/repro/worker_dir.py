from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ..utils.jsonl import write_text
from ..utils.paths import ensure_dir, is_within, safe_relative_path
from ..utils.process import run_process


def create_worker_dir(checkout: Path, worker: Path, candidate: dict) -> Path:
    _reset_worker_dir(worker)
    ensure_dir(worker.parent)
    ensure_dir(worker)
    repo = worker / "repo"
    if (checkout / ".git").exists():
        clone = run_process(
            ["git", "clone", "--shared", str(checkout), str(repo)],
            cwd=checkout,
            timeout=600,
        )
        if clone.returncode != 0:
            raise RuntimeError(f"git clone --shared failed: {(clone.stderr or clone.stdout)[-500:]}")
        current_commit = run_process(["git", "rev-parse", "HEAD"], cwd=checkout, timeout=60)
        if current_commit.returncode == 0 and current_commit.stdout.strip():
            checkout_current = run_process(["git", "checkout", "--detach", current_commit.stdout.strip()], cwd=repo, timeout=120)
            if checkout_current.returncode != 0:
                raise RuntimeError(f"worker checkout failed: {(checkout_current.stderr or checkout_current.stdout)[-500:]}")
        _remove_symlinks(repo)
    else:
        _copy_snapshot_tree(checkout, repo)
    for child in ("logs", "repro", "home", "tmp", "cache"):
        ensure_dir(worker / child)
    payload = json.dumps(candidate, ensure_ascii=False, indent=2)
    write_text(worker / "input_candidate.json", payload)
    write_text(worker / "candidate.json", payload)
    return worker


def _copy_snapshot_tree(checkout: Path, repo: Path) -> None:
    if repo.exists() and not repo.is_symlink():
        shutil.rmtree(repo)
    elif repo.is_symlink():
        repo.unlink()
    ensure_dir(repo)
    try:
        checkout_root = checkout.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"snapshot source is unavailable: {checkout}") from exc
    for root, dirs, names in os.walk(checkout, followlinks=False):
        root_path = Path(root)
        try:
            rel_root = root_path.relative_to(checkout).as_posix()
        except ValueError:
            continue
        ignored = _copytree_ignore(str(root_path), [*dirs, *names])
        dirs[:] = [
            name
            for name in dirs
            if name not in ignored and not (root_path / name).is_symlink()
        ]
        for name in names:
            if name in ignored:
                continue
            source = root_path / name
            if source.is_symlink() or not source.is_file():
                continue
            rel = safe_relative_path(source.relative_to(checkout).as_posix())
            if not rel or _path_has_symlink_component(checkout, rel):
                continue
            try:
                resolved = source.resolve(strict=True)
            except OSError:
                continue
            if not is_within(resolved, checkout_root):
                continue
            destination = repo / rel
            ensure_dir(destination.parent)
            shutil.copy2(source, destination)


def _reset_worker_dir(worker: Path) -> None:
    if not worker.exists() and not worker.is_symlink():
        return
    if worker.is_symlink():
        worker.unlink()
        return
    if not is_within(worker, worker.parent):
        raise RuntimeError(f"refusing to remove worker outside worker root: {worker}")
    if worker.is_dir():
        shutil.rmtree(worker)
    else:
        worker.unlink()


def _remove_symlinks(root: Path) -> None:
    for current, dirs, names in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in names:
            path = current_path / name
            if path.is_symlink():
                path.unlink(missing_ok=True)
        for name in dirs:
            path = current_path / name
            if path.is_symlink():
                path.unlink(missing_ok=True)


def _path_has_symlink_component(root: Path, rel: object) -> bool:
    safe = safe_relative_path(rel)
    if not safe:
        return False
    current = root
    for part in Path(safe).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _copytree_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {".git", "node_modules", ".venv", "__pycache__"} & set(names)
    if Path(directory).name == ".codereview":
        ignored.update({"runs"} & set(names))
    return ignored