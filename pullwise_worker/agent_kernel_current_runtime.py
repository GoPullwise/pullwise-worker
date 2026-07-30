"""Production composition root for current-package R0 execution."""

from __future__ import annotations

from typing import Callable

from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_package import (
    CURRENT_TOOL_CATALOG,
    CurrentInvocationCodec,
    ServerAuthorityEnvelope,
)
from .agent_kernel_current_policy import CurrentR0Policy
from .agent_kernel_current_r0_execution import CurrentR0ExecutionAdapter
from .agent_kernel_dispatch_journal import CurrentDispatchJournal
from .agent_kernel_gateway import AgentKernelGateway, GatewayError
from .agent_kernel_r0_capture import MaterializedSourceCaptureProvider
from .agent_kernel_r0_read import R0ReadPreparer


class CurrentRuntimeRunner:
    """Run one exact current authority/request pair through the real Gateway."""

    def __init__(
        self,
        database: CurrentAgentKernelDatabase,
        *,
        capture_provider: MaterializedSourceCaptureProvider,
        base_revision: str,
        max_read_bytes: int,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if clock is not None and not callable(clock):
            raise GatewayError("CURRENT_RUNTIME_CLOCK_INVALID")
        self.journal = CurrentDispatchJournal(database, clock=clock)
        self.preparer = R0ReadPreparer(
            capture_provider=capture_provider,
            base_revision=base_revision,
            max_bytes=max_read_bytes,
        )
        self.execution = CurrentR0ExecutionAdapter(
            self.journal,
            clock=clock,
        )
        self.policy = CurrentR0Policy()

    def run_r0(self, authority_bytes: bytes, request_bytes: bytes) -> bytes:
        authority = ServerAuthorityEnvelope.from_canonical_bytes(authority_bytes)
        recorded = self.journal.record_authority(authority)
        if not isinstance(recorded, ServerAuthorityEnvelope):
            raise GatewayError("CURRENT_AUTHORITY_INVALID")
        gateway = AgentKernelGateway(
            codec=CurrentInvocationCodec(
                recorded,
                self.journal.resolve_authority,
            ),
            journal=self.journal,
            authority=self.journal,
            catalog=CURRENT_TOOL_CATALOG,
            policy=self.policy,
            preparer=self.preparer,
            budget=self.journal,
            dispatcher=self.execution,
            committer=self.execution,
        )
        result = gateway.invoke(request_bytes)
        if not isinstance(result, bytes):
            raise GatewayError("CURRENT_RUNTIME_RESULT_INVALID")
        return result


__all__ = ["CurrentRuntimeRunner"]
