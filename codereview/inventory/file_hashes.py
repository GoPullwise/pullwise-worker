from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path


@dataclass(frozen=True)
class FileAnalysis:
    binary: bool
    line_count: int
    content_hash: str


def _open_no_follow(path: Path, mode: str, **kwargs):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    return os.fdopen(fd, mode, **kwargs)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _open_no_follow(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def looks_binary(path: Path, *, sample_size: int = 4096) -> bool:
    try:
        with _open_no_follow(path, "rb") as handle:
            sample = handle.read(sample_size)
    except OSError:
        return True
    return b"\x00" in sample


def line_count(path: Path) -> int:
    try:
        with _open_no_follow(path, "r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def analyze_file(path: Path, *, binary_sample_size: int = 4096) -> FileAnalysis:
    digest = hashlib.sha256()
    line_total = 0
    binary = False
    saw_data = False
    last_byte = b""
    sampled = 0
    try:
        with _open_no_follow(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                if not chunk:
                    continue
                digest.update(chunk)
                saw_data = True
                last_byte = chunk[-1:]
                line_total += chunk.count(b"\n")
                if sampled < binary_sample_size:
                    sample = chunk[: binary_sample_size - sampled]
                    sampled += len(sample)
                    if b"\x00" in sample:
                        binary = True
        if saw_data and last_byte != b"\n":
            line_total += 1
        return FileAnalysis(
            binary=binary,
            line_count=0 if binary else line_total,
            content_hash=f"sha256:{digest.hexdigest()}",
        )
    except OSError:
        return FileAnalysis(binary=True, line_count=0, content_hash="")
