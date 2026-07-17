from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.agent_first_decision_gate import normative_reference_failures
from scripts.agent_first_decision_register import validate_register
from tests.test_agent_first_decision_register_gate import (
    _resolve,
    _resolved_d1,
    _write_normative_docs,
)


class AgentFirstDecisionRegisterProvenanceTest(unittest.TestCase):
    def test_partially_resolved_units_require_the_resolved_prefix(self) -> None:
        generic = _resolved_d1("generic_agent_worker")
        d3 = _resolved_d1()
        option_id = d3["decisions"][2]["options"][0]["id"]
        d3 = _resolve(d3, "D3", option_id)
        d3["active_decision_id"] = "D4"
        validate_register(d3)
        cases = (
            (generic, "target-authority-scope"),
            (d3, "mvp-contract-pack"),
        )
        for register, expected_unit in cases:
            with self.subTest(unit=expected_unit):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    _write_normative_docs(
                        root, register, include_ready=False
                    )
                    missing = normative_reference_failures(
                        register, root, require_slice=None
                    )
                    self.assertIn(
                        expected_unit,
                        {
                            item["unit_id"]
                            for item in missing
                            if item["code"]
                            == "normative_unit_reference_missing"
                        },
                    )
                    _write_normative_docs(root, register)
                    self.assertEqual(
                        [],
                        normative_reference_failures(
                            register, root, require_slice=None
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
