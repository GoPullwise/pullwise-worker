"""Filesystem-operand parsing and containment for agent-proposed commands."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


_COMMAND_PATH_OPTIONS = {
    "cargo": (("--target-dir", False),),
    "go": (("-coverprofile", False),),
    "make": (("-C", True), ("--directory", False)),
    "pytest": (("-c", True), ("--basetemp", False)),
}


def _looks_like_path_operand(value: object, *, cwd: Path) -> bool:
    argument = str(value or "").strip()
    if not argument:
        return False
    candidate = Path(argument)
    if (
        candidate.is_absolute()
        or argument in {".", ".."}
        or "/" in argument
        or "\\" in argument
    ):
        return True
    try:
        return (cwd / candidate).is_symlink()
    except (OSError, ValueError):
        return True


def command_path_operands(command: Iterable[object], *, cwd: Path) -> tuple[str, ...]:
    """Extract filesystem operands using a small test-runner safety grammar."""

    argv = [str(part).strip() for part in command if str(part).strip()]
    if not argv:
        return ()
    executable = Path(argv[0]).name.lower().removesuffix(".exe")
    index = 1
    is_python = executable in {"py", "python", "python3"} or executable.startswith("python3.")
    if is_python and len(argv) >= 3 and argv[1] == "-m" and argv[2].lower() == "pytest":
        executable, index = "pytest", 3
    options = dict(_COMMAND_PATH_OPTIONS.get(executable, ()))
    opaque_options = {"-run"} if executable == "go" else set()
    operands: list[str] = []
    options_finished = False
    while index < len(argv):
        argument = argv[index]
        if not options_finished and argument == "--":
            options_finished = True
            index += 1
            continue
        if not options_finished:
            if argument in options:
                if index + 1 < len(argv):
                    operands.append(argv[index + 1])
                index += 2
                continue
            if argument in opaque_options:
                index += 2
                continue
            option, separator, value = argument.partition("=")
            if separator:
                if option in options or (
                    option not in opaque_options
                    and _looks_like_path_operand(value, cwd=cwd)
                ):
                    operands.append(value)
                index += 1
                continue
            attached = next(
                (
                    argument[len(option) :]
                    for option, allowed in options.items()
                    if allowed and argument.startswith(option)
                ),
                "",
            )
            if attached:
                operands.append(attached)
                index += 1
                continue
            if argument.startswith("-"):
                index += 1
                continue
        if _looks_like_path_operand(argument, cwd=cwd):
            operands.append(argument)
        index += 1
    return tuple(operand for operand in operands if operand)


def command_path_operand_containment(
    value: object,
    *,
    cwd: Path,
    validation_root: Path,
) -> bool | None:
    """Return containment for an extracted path operand, otherwise ``None``."""

    argument = str(value or "").strip()
    if not argument:
        return None
    if not _looks_like_path_operand(argument, cwd=cwd):
        return None
    candidate = Path(argument)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        candidate.resolve(strict=False).relative_to(validation_root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def command_argument_path_containment(
    value: object,
    *,
    cwd: Path,
    validation_root: Path,
) -> bool | None:
    """Return containment for a recognizable raw argument, otherwise ``None``."""

    argument = str(value or "").strip()
    if argument.startswith("-"):
        _option, separator, argument = argument.partition("=")
        if not separator:
            return None
        argument = argument.strip()
    return command_path_operand_containment(
        argument,
        cwd=cwd,
        validation_root=validation_root,
    )
