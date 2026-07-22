"""Exact prepared-dispatch admission checks for the current journal."""

from __future__ import annotations

from .agent_kernel_current_journal_types import CurrentJournalError
from .agent_kernel_gateway import CheckedInvocation, PreparedDispatch, ToolDescriptor
from .agent_kernel_source_state import SourceTreeSnapshot


def assert_prepared_dispatch(
    call: CheckedInvocation,
    descriptor: ToolDescriptor,
    prepared: PreparedDispatch,
) -> str:
    if (
        not isinstance(prepared, PreparedDispatch)
        or prepared.tool_key != descriptor.tool_key
        or prepared.tool_version != descriptor.tool_version
        or not isinstance(prepared.source_before, SourceTreeSnapshot)
    ):
        raise CurrentJournalError("PREPARED_DISPATCH_INVALID")
    value = call.tool_input
    relative_path = (
        value.get("relative_path")
        if isinstance(value, dict)
        else getattr(value, "relative_path", None)
    )
    if not isinstance(relative_path, str) or not relative_path:
        raise CurrentJournalError("R0_READ_PATH_INVALID")
    return relative_path


__all__ = ["assert_prepared_dispatch"]
