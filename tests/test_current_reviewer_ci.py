from __future__ import annotations

from pathlib import Path
import re
import unittest


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]

WORKFLOWS = {
    "admin": WORKSPACE_ROOT / "pullwise-admin" / ".github" / "workflows" / "ci.yml",
    "server": WORKSPACE_ROOT / "pullwise-server" / ".github" / "workflows" / "ci.yml",
    "web": WORKSPACE_ROOT / "pullwise-web" / ".github" / "workflows" / "ci.yml",
    "worker": WORKSPACE_ROOT / "pullwise-worker" / ".github" / "workflows" / "ci.yml",
}

CHECKER_PATHS = {
    "admin": WORKSPACE_ROOT / "pullwise-admin" / "scripts" / "check-reviewer-authority.mjs",
    "server": WORKSPACE_ROOT
    / "pullwise-server"
    / "scripts"
    / "check_current_reviewer_authority.py",
    "web": WORKSPACE_ROOT / "pullwise-web" / "scripts" / "check-reviewer-authority.mjs",
    "worker": WORKSPACE_ROOT
    / "pullwise-worker"
    / "scripts"
    / "check_current_reviewer_authority.py",
}

CHECKER_COMMANDS = {
    "admin": "node scripts/check-reviewer-authority.mjs",
    "server": "python scripts/check_current_reviewer_authority.py",
    "web": "node scripts/check-reviewer-authority.mjs",
    "worker": "python scripts/check_current_reviewer_authority.py --repo worker",
}

EXACT_PACKAGE_TEST = (
    WORKSPACE_ROOT / "pullwise-worker" / "tests" / "test_agent_kernel_current_package.py"
)

FIRST_BLOCKING_COMMANDS = {
    "admin": "npm ci",
    "server": 'python -m pip install --upgrade "pip>=25.3,<26" setuptools wheel',
    "web": "npm ci",
    "worker": 'python -m pip install --upgrade "pip>=25.3,<27" setuptools wheel',
}

NORMAL_BLOCKING_COMMANDS = {
    "admin": (
        "npm run check",
        "npm audit --omit=dev --audit-level=high",
        "node --check worker.js",
        "node --check 'functions/api/[[path]].js'",
    ),
    "server": (
        "pip-audit .",
        "python -m pip check",
        "bash -n ./launcher.sh",
        "bash -n ./git-watch.sh",
        "bash -n ./ops/configure_api_proxy.sh",
        "python -m pytest",
    ),
    "web": (
        "npm run check",
        "npm audit --omit=dev",
        "node --check worker.js",
    ),
    "worker": (
        "pip-audit .",
        "python -m pip check",
        "bash -n ./deploy/update-worker.sh",
        "bash -n ./deploy/restart-worker.sh",
        "bash -n ./deploy/uninstall-worker.sh",
        "bash -n ./deploy/cleanup-checkouts.sh",
        "python scripts/verify_agent_first_legacy_absence.py --workspace-root ..",
        "python scripts/check_output_contracts.py",
        "python scripts/check_agent_kernel_wheel.py",
        'python -m unittest discover -s tests -p "test_*.py"',
    ),
}


def workflow(repo: str) -> str:
    return WORKFLOWS[repo].read_text(encoding="utf-8").replace("\r\n", "\n")


def named_step(text: str, name: str) -> str:
    escaped = re.escape(name)
    pattern = re.compile(
        rf'(?ms)^      - name: (?:"{escaped}"|{escaped})\n'
        rf"(.*?)(?=^      - (?:name:|uses:)|\Z)"
    )
    match = pattern.search(text)
    if match is None:
        raise AssertionError(f"workflow step missing: {name}")
    return match.group(0)


class CurrentReviewerCiTest(unittest.TestCase):
    def test_every_workflow_runs_its_local_checker_before_install_or_tests(self) -> None:
        for repo, command in CHECKER_COMMANDS.items():
            with self.subTest(repo=repo):
                text = workflow(repo)
                self.assertTrue(CHECKER_PATHS[repo].is_file())
                self.assertIn(command, text)
                self.assertLess(text.index(command), text.index(FIRST_BLOCKING_COMMANDS[repo]))

    def test_workflows_pin_the_card_runtime(self) -> None:
        for repo in ("admin", "web"):
            with self.subTest(repo=repo):
                self.assertIn('node-version: "22.12.0"', workflow(repo))
        for repo in ("server", "worker"):
            with self.subTest(repo=repo):
                self.assertIn('python-version: "3.10.12"', workflow(repo))
        for repo in WORKFLOWS:
            with self.subTest(repo=repo):
                self.assertIn("runs-on: ubuntu-22.04", workflow(repo))

    def test_normal_regression_audit_syntax_and_build_commands_stay_blocking(self) -> None:
        for repo, commands in NORMAL_BLOCKING_COMMANDS.items():
            text = workflow(repo)
            for command in commands:
                with self.subTest(repo=repo, command=command):
                    self.assertIn(command, text)
                    line = next(line for line in text.splitlines() if command in line)
                    self.assertNotIn("|| true", line)

    def test_workflows_are_read_only_and_do_not_deploy_or_push(self) -> None:
        forbidden = (
            "git push",
            "npm publish",
            "docker push",
            "wrangler deploy",
            "cloudflare deploy",
            "kubectl ",
            "ssh ",
        )
        for repo in WORKFLOWS:
            with self.subTest(repo=repo):
                text = workflow(repo)
                self.assertIn("permissions:\n  contents: read", text)
                for literal in forbidden:
                    self.assertNotIn(literal, text.lower())

    def test_worker_checks_out_admin_before_full_cross_repository_tests(self) -> None:
        text = workflow("worker")
        admin_checkout = named_step(text, "Check out current Admin test dependency")
        run_tests = named_step(text, "Run tests")

        self.assertIn("uses: actions/checkout@v4", admin_checkout)
        self.assertIn("repository: GoPullwise/pullwise-admin", admin_checkout)
        self.assertIn("path: pullwise-admin", admin_checkout)
        self.assertIn("persist-credentials: false", admin_checkout)
        self.assertLess(text.index(admin_checkout), text.index(run_tests))

    def test_worker_frozen_and_exact_package_steps_are_diagnostic_only(self) -> None:
        text = workflow("worker")
        diagnostic_names = (
            "Diagnostic (non-authoritative): frozen Server checkout",
            "Diagnostic (non-authoritative): current exact-package Server checkout",
            "Diagnostic (non-authoritative): frozen Web checkout",
            "Diagnostic (non-authoritative): Check current Agent-First package exact lock",
        )
        for name in diagnostic_names:
            with self.subTest(step=name):
                step = named_step(text, name)
                self.assertIn("continue-on-error: true", step)

        exact_package_step = named_step(
            text,
            "Diagnostic (non-authoritative): Check current Agent-First package exact lock",
        )
        self.assertIn(
            'PULLWISE_EXPERIMENTAL_EXACT_PACKAGE_DIAGNOSTIC: "1"',
            exact_package_step,
        )
        self.assertIn(
            "python -m unittest tests.test_agent_kernel_current_package",
            exact_package_step,
        )

        run_tests = named_step(text, "Run tests")
        self.assertNotIn("PULLWISE_CURRENT_SERVER_ROOT", run_tests)
        self.assertNotIn(
            "PULLWISE_EXPERIMENTAL_EXACT_PACKAGE_DIAGNOSTIC",
            run_tests,
        )

        exact_package_test = EXACT_PACKAGE_TEST.read_text(encoding="utf-8")
        self.assertIn("@unittest.skipUnless(", exact_package_test)
        self.assertIn(
            "os.environ.get('PULLWISE_EXPERIMENTAL_EXACT_PACKAGE_DIAGNOSTIC') == '1'",
            exact_package_test,
        )

    def test_worker_target_paths_do_not_use_frozen_refs_as_authority(self) -> None:
        text = workflow("worker")
        authority_step = named_step(text, "Check current Reviewer authority")
        run_tests = named_step(text, "Run tests")
        for step in (authority_step, run_tests):
            self.assertNotIn("PULLWISE_CURRENT_SERVER_ROOT", step)
            self.assertNotIn("current-contract-server", step)
            self.assertNotIn("pullwise-server", step)
            self.assertNotIn("pullwise-web", step)


if __name__ == "__main__":
    unittest.main()
