from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..utils.jsonl import write_text
from ..utils.paths import ensure_dir
from ..utils.paths import is_within
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
    else:
        shutil.copytree(checkout, repo, ignore=_copytree_ignore)
    for child in ("logs", "repro", "home", "tmp", "cache"):
        ensure_dir(worker / child)
    payload = json.dumps(candidate, ensure_ascii=False, indent=2)
    write_text(worker / "input_candidate.json", payload)
    write_text(worker / "candidate.json", payload)
    return worker


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


def _copytree_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {".git", "node_modules", ".venv", "__pycache__"} & set(names)
    if Path(directory).name == ".codereview":
        ignored.update({"runs"} & set(names))
    return ignored
