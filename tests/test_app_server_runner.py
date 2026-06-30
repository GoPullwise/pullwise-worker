from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codereview.app_server_runner import (
    AppServerTurn,
    app_server_max_age_seconds,
    app_server_max_turns,
    run_codex_app_server_turn,
)
from codereview.config import CodexConfig


class AppServerRunnerTests(unittest.TestCase):
    def test_app_server_recycles_by_conservative_default_limits(self) -> None:
        self.assertEqual(app_server_max_age_seconds({}), 2400)
        self.assertEqual(app_server_max_turns({}), 48)
        self.assertEqual(
            app_server_max_age_seconds({"PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS": "45"}),
            45,
        )
        self.assertEqual(app_server_max_turns({"PULLWISE_CODEX_APP_SERVER_MAX_TURNS": "3"}), 3)

    def test_turn_timeout_does_not_close_shared_app_server(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.closed = False

            def run_turn(self, **kwargs):
                del kwargs
                raise TimeoutError("codex app-server turn timed out after 60s")

            def close(self, reason: str = "") -> None:
                del reason
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            fake = FakeClient()
            with patch("codereview.app_server_runner.get_codex_app_server_client", return_value=fake):
                result = run_codex_app_server_turn(
                    cd=root,
                    prompt="prompt",
                    output_schema=schema,
                    output_file=root / "out.json",
                    sandbox="read-only",
                    timeout_seconds=60,
                    config=CodexConfig(command="codex"),
                )
            self.assertEqual(result.returncode, 2)
            self.assertIn("timed out", result.stderr)
            self.assertFalse(fake.closed)

    def test_auth_turn_error_closes_shared_app_server_with_root_cause(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.close_reason = ""

            def run_turn(self, **kwargs):
                del kwargs
                return AppServerTurn(thread_id="thread", error="Failed to refresh token: refresh token was already used")

            def close(self, reason: str = "") -> None:
                self.close_reason = reason

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            fake = FakeClient()
            with patch("codereview.app_server_runner.get_codex_app_server_client", return_value=fake):
                result = run_codex_app_server_turn(
                    cd=root,
                    prompt="prompt",
                    output_schema=schema,
                    output_file=root / "out.json",
                    sandbox="read-only",
                    timeout_seconds=60,
                    config=CodexConfig(command="codex"),
                )
            self.assertEqual(result.returncode, 1)
            self.assertIn("refresh token", fake.close_reason.lower())


if __name__ == "__main__":
    unittest.main()
