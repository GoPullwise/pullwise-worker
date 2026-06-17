from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..utils.paths import ensure_dir
from ..utils.paths import is_within
from ..utils.process import run_process


def create_worker_dir(checkout: Path, worker: Path, candidate: dict) -> Path:
    if worker.exists():
        if not is_within(worker, worker.parent):
            raise RuntimeError(f"refusing to remove worker outside worker root: {worker}")
        shutil.rmtree(worker)
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
        head = run_process(["git", "rev-parse", "HEAD"], cwd=checkout, timeout=60)
        if head.returncode == 0 and head.stdout.strip():
            checkout_head = run_process(["git", "checkout", "--detach", head.stdout.strip()], cwd=repo, timeout=120)
            if checkout_head.returncode != 0:
                raise RuntimeError(f"worker checkout failed: {(checkout_head.stderr or checkout_head.stdout)[-500:]}")
    else:
        shutil.copytree(checkout, repo, ignore=_copytree_ignore)
    for child in ("logs", "repro", "home", "tmp", "cache"):
        ensure_dir(worker / child)
    payload = json.dumps(candidate, ensure_ascii=False, indent=2)
    (worker / "input_candidate.json").write_text(payload, encoding="utf-8")
    (worker / "candidate.json").write_text(payload, encoding="utf-8")
    return worker


def _copytree_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {".git", ".codegraph", "node_modules", ".venv", "__pycache__"} & set(names)
    if Path(directory).name == ".codereview":
        ignored.update({"runs", "codegraph-index"} & set(names))
    return ignored
