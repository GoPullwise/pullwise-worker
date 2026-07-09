from __future__ import annotations

import argparse
import sys

from ._main_part_01_bootstrap import PullwiseClient, WorkerConfig
from .review_worker_v1 import ReviewWorkerV1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Pullwise Codex review worker.")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=[
            "run",
            "doctor",
            "start",
            "stop",
            "status",
            "logs",
            "restart",
            "update",
            "uninstall",
            "finalize-uninstall",
            "watch",
            "cleanup",
                    "codex-login",
        ],
    )
    parser.add_argument("--server-url")
    parser.add_argument("--worker-id")
    parser.add_argument("--poll-seconds", type=int)
    parser.add_argument("--work-dir")
    parser.add_argument("--checkout-root")
    parser.add_argument("--log-dir")
    parser.add_argument("--provider")
    parser.add_argument("--codex-command")
    parser.add_argument("--lines", type=int, default=120)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--remove-config", action="store_true")
    parser.add_argument("--remove-logs", action="store_true")
    return parser


def _config(args: argparse.Namespace, *, require_worker_token: bool, validate_server_url: bool) -> WorkerConfig:
    return WorkerConfig(args, require_worker_token=require_worker_token, validate_server_url=validate_server_url)


def main() -> None:
    args = build_parser().parse_args()

    try:
        if args.command in {"start", "stop", "status", "restart", "logs"}:
            from ._main_part_08_lifecycle_cleanup import service_action, worker_logs

            config = _config(args, require_worker_token=False, validate_server_url=False)
            if args.command == "logs":
                raise SystemExit(worker_logs(config, lines=args.lines, follow=args.follow, dry_run=args.dry_run))
            raise SystemExit(service_action(args.command, dry_run=args.dry_run, config=config))

        if args.command == "uninstall":
            from ._main_part_08_lifecycle_cleanup import uninstall_worker_command

            raise SystemExit(uninstall_worker_command(args))

        if args.command == "finalize-uninstall":
            from ._main_part_08_lifecycle_cleanup import finalize_worker_uninstall

            config = _config(args, require_worker_token=False, validate_server_url=False)
            raise SystemExit(finalize_worker_uninstall(config, dry_run=args.dry_run))

        if args.command == "watch":
            from ._main_part_08_lifecycle_cleanup import run_lifecycle_watcher

            config = _config(args, require_worker_token=True, validate_server_url=True)
            raise SystemExit(run_lifecycle_watcher(config, once=args.once))

        require_worker_token = args.command in {"run", "doctor"}
        config = _config(
            args,
            require_worker_token=require_worker_token,
            validate_server_url=args.command not in {"update", "cleanup", "codex-login"},
        )

        if args.command == "doctor":
            from ._main_part_07_readiness_doctor import run_doctor

            raise SystemExit(0 if run_doctor(config) else 1)

        
        if args.command == "codex-login":
            from ._main_part_07_readiness_doctor import run_codex_device_login

            raise SystemExit(0 if run_codex_device_login(config) else 1)
if args.command == "update":
            from ._main_part_08_lifecycle_cleanup import update_worker

            raise SystemExit(update_worker(config, dry_run=args.dry_run))

        if args.command == "cleanup":
            from ._main_part_08_lifecycle_cleanup import cleanup_worker_resources

            cleanup_worker_resources(config)
            raise SystemExit(0)

        worker = ReviewWorkerV1(config, client=PullwiseClient(config))
        worker.run(once=args.once)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    main()
