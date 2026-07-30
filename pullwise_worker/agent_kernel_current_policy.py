"""Fail-closed preflight policy for the current R0 tool surface."""

from __future__ import annotations

from .agent_kernel_current_package import (
    CURRENT_TOOL_CATALOG,
    ServerAuthorityEnvelope,
)
from .agent_kernel_gateway_contracts import (
    CheckedInvocation,
    GatewayError,
    PreparedDispatch,
    ToolDescriptor,
)


class CurrentR0Policy:
    """Authorize only the exact current-package R0 descriptor and grant."""

    @staticmethod
    def assert_capability(
        ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
    ) -> None:
        if not isinstance(ticket, ServerAuthorityEnvelope):
            raise GatewayError("DISPATCH_NOT_AUTHORIZED")
        try:
            catalog_descriptor = CURRENT_TOOL_CATALOG.resolve(call.tool_key)
        except GatewayError as exc:
            raise GatewayError("DISPATCH_NOT_AUTHORIZED") from exc
        if (
            descriptor != catalog_descriptor
            or descriptor.tool_key != call.tool_key
            or descriptor.tool_key not in ticket.grant.tool_keys
            or descriptor.capability not in ticket.grant.capability_ids
        ):
            raise GatewayError("DISPATCH_NOT_AUTHORIZED")

    @classmethod
    def assert_execution_controls(
        cls,
        ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
        prepared: PreparedDispatch,
    ) -> None:
        cls.assert_capability(ticket, call, descriptor)
        if descriptor.risk != "R0":
            raise GatewayError("CAPABILITY_NOT_IMPLEMENTED")
        if (
            descriptor.uses_command
            or descriptor.uses_network
            or descriptor.uses_secret
            or descriptor.requests_approval
        ):
            raise GatewayError("POLICY_INVARIANT_BROKEN")
        if (
            prepared.tool_key != descriptor.tool_key
            or prepared.tool_version != descriptor.tool_version
        ):
            raise GatewayError("PREPARED_DISPATCH_INVALID")


__all__ = ["CurrentR0Policy"]
