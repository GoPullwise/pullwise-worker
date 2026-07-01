from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .legacy import get_legacy_simple_review
from .runner import run_review


def main(argv: list[str] | None = None) -> int:
    legacy = get_legacy_simple_review()
    parser = argparse.ArgumentParser(prog="python -m codereview")
    sub = parser.add_subparsers(dest="command", required=True)
    init_parser = sub.add_parser("init")
    init_parser.add_argument("--checkout", default=".")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--checkout", default=".")
    run_parser.add_argument("--mode", choices=["fast", "standard", "deep"], default="")
    run_parser.add_argument("--scan-mode", choices=["full-cached", "full-strict"], default="")
    args = parser.parse_args(argv)
    checkout = Path(args.checkout)
    if args.command == "init":
        print(legacy.init_project(checkout))
        return 0
    try:
        final = run_review(checkout, mode=args.mode, scan_mode=args.scan_mode)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 5
    print(final)
    try:
        confirmed = json.loads(final.with_name("confirmed.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 5
    return 1 if isinstance(confirmed, list) and confirmed else 0
