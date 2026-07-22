from __future__ import annotations

from dataclasses import fields
import unittest

from pullwise_worker.agent_kernel_gateway import CheckedInvocation


class AgentKernelGatewayContractsTest(unittest.TestCase):
    def test_checked_invocation_has_no_agent_controlled_observation_fields(self) -> None:
        names = {item.name for item in fields(CheckedInvocation)}
        forbidden = {
            "status",
            "started_at",
            "completed_at",
            "source_state_before_id",
            "source_state_after_id",
            "observation_id",
        }

        self.assertTrue(forbidden.isdisjoint(names))


if __name__ == "__main__":
    unittest.main()
