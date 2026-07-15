from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pullwise_worker import _main_part_07_readiness_doctor as readiness_doctor


class DeviceLoginRegressionsTest(unittest.TestCase):
    def test_device_login_prepares_instance_scoped_codex_home_before_sdk_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker_root = Path(tmp_dir) / "worker"
            codex_home = worker_root / "codex-home"
            codex_sqlite_home = worker_root / "codex-sqlite"
            config = SimpleNamespace(
                worker_id="wk_device_login",
                worker_token="",
                service_home=str(Path(tmp_dir) / "service"),
                worker_root=worker_root,
                codex_home=codex_home,
                codex_sqlite_home=codex_sqlite_home,
                service_path="/usr/local/bin:/usr/bin",
                codex_command="",
            )
            observed: dict[str, object] = {}

            class FakeCodexClient:
                def __init__(
                    self,
                    command: str,
                    env: dict[str, str],
                    cwd: Path,
                    events_path: Path,
                ) -> None:
                    del command, cwd, events_path
                    observed["env"] = env

                def start(self) -> None:
                    observed["started"] = True
                    self.assert_runtime_prepared()

                @staticmethod
                def assert_runtime_prepared() -> None:
                    if not codex_home.is_dir():
                        raise AssertionError("CODEX_HOME was not created before SDK start")
                    if not codex_sqlite_home.is_dir():
                        raise AssertionError("CODEX_SQLITE_HOME was not created before SDK start")
                    if not (codex_home / "config.toml").is_file():
                        raise AssertionError("isolated Codex credential-store config was not created")

                def login_chatgpt_device_code(self) -> SimpleNamespace:
                    return SimpleNamespace(
                        verification_url="https://example.test/device",
                        user_code="ABCD-EFGH",
                        wait=lambda: SimpleNamespace(success=True),
                    )

                def close(self) -> None:
                    observed["closed"] = True

            with patch.object(readiness_doctor, "CodexSdkClient", FakeCodexClient):
                success = readiness_doctor.run_codex_device_login(config)

        self.assertTrue(success)
        self.assertTrue(observed.get("started"))
        self.assertTrue(observed.get("closed"))
        env = observed.get("env")
        self.assertIsInstance(env, dict)
        assert isinstance(env, dict)
        self.assertEqual(env["HOME"], str(worker_root))
        self.assertEqual(env["CODEX_HOME"], str(codex_home))


if __name__ == "__main__":
    unittest.main()
