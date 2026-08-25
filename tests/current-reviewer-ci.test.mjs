import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const WORKSPACE_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
);
const NODE_VERSION = "22.23.1";
const CURRENT_BLOCK_SHA256 =
  "24435fb38cf3b04c77243fb20df00fc3ceb928ebe560e4b8682c8e3c8f36deeb";
const TARGET_BLOCK_SHA256 =
  "c61800c199d637568022d730f0758c7c523f44009a1aed66be20ea5034ef5eaa";

const WORKFLOW_PATHS = Object.freeze({
  admin: "pullwise-admin/.github/workflows/ci.yml",
  server: "pullwise-server/.github/workflows/ci.yml",
  web: "pullwise-web/.github/workflows/ci.yml",
  worker: "pullwise-worker/.github/workflows/ci.yml",
});
const CHECKER_PATHS = Object.freeze({
  admin: "pullwise-admin/scripts/check-reviewer-authority.mjs",
  server: "pullwise-server/scripts/check_current_reviewer_authority.py",
  web: "pullwise-web/scripts/check-reviewer-authority.mjs",
  worker: "pullwise-worker/scripts/check-current-reviewer-target.mjs",
});
const LOCAL_CHECK_COMMANDS = Object.freeze({
  admin: "node scripts/check-reviewer-authority.mjs",
  server: "python scripts/check_current_reviewer_authority.py",
  web: "node scripts/check-reviewer-authority.mjs",
  worker: "node scripts/check-current-reviewer-target.mjs --repo worker",
});
const FIRST_INSTALL_COMMANDS = Object.freeze({
  admin: "npm ci",
  server: 'python -m pip install --upgrade "pip>=25.3,<26" setuptools wheel',
  web: "npm ci",
  worker: 'python -m pip install --upgrade "pip>=25.3,<27" setuptools wheel',
});
const FORBIDDEN = Object.freeze([
  "git push",
  "npm publish",
  "docker push",
  "wrangler deploy",
  "cloudflare deploy",
  "kubectl ",
  "ssh ",
]);
const HISTORICAL_LABEL =
  "Historical cleanup regression (blocking until R1-PI-04)";
const LEGACY_PYTHON_TARGET_PATHS = Object.freeze([
  "pullwise-worker/scripts/check_current_reviewer_authority.py",
  "pullwise-worker/tests/test_current_reviewer_authority.py",
  "pullwise-worker/tests/test_current_reviewer_ci.py",
]);

function read(relativePath) {
  return fs.readFileSync(path.join(WORKSPACE_ROOT, relativePath), "utf8")
    .replace(/\r\n?/g, "\n");
}

function workflows() {
  return Object.fromEntries(
    Object.entries(WORKFLOW_PATHS).map(([repo, relativePath]) => [repo, read(relativePath)]),
  );
}

function namedStep(text, name) {
  const candidates = [
    `      - name: ${name}\n`,
    `      - name: "${name}"\n`,
  ];
  const start = candidates.map((candidate) => text.indexOf(candidate))
    .find((index) => index >= 0);
  if (start === undefined) return null;
  const next = text.indexOf("\n      - ", start + 1);
  return text.slice(start, next < 0 ? text.length : next);
}

function addOrderingError(errors, repo, text, before, after) {
  const beforeIndex = text.indexOf(before);
  const afterIndex = text.indexOf(after);
  if (beforeIndex < 0) errors.push(`${repo}:missing:${before}`);
  if (afterIndex < 0) errors.push(`${repo}:missing:${after}`);
  if (beforeIndex >= 0 && afterIndex >= 0 && beforeIndex >= afterIndex) {
    errors.push(`${repo}:order:${before}`);
  }
}

function validateWorkflow(repo, text) {
  const errors = [];
  if (!text.includes("permissions:\n  contents: read")) {
    errors.push(`${repo}:permissions_not_read_only`);
  }
  const lowered = text.toLowerCase();
  for (const command of FORBIDDEN) {
    if (lowered.includes(command)) errors.push(`${repo}:forbidden:${command.trim()}`);
  }
  const nodeVersions = [...text.matchAll(/node-version:\s*["']?([^"'\s]+)["']?/g)]
    .map((match) => match[1]);
  if (["admin", "web", "worker"].includes(repo)) {
    if (nodeVersions.length !== 1 || nodeVersions[0] !== NODE_VERSION) {
      errors.push(`${repo}:node_version`);
    }
  } else if (nodeVersions.some((version) => version !== NODE_VERSION)) {
    errors.push(`${repo}:node_version`);
  }
  addOrderingError(
    errors,
    repo,
    text,
    LOCAL_CHECK_COMMANDS[repo],
    FIRST_INSTALL_COMMANDS[repo],
  );

  if (repo === "worker") {
    const workspaceCheck =
      "node scripts/check-current-reviewer-target.mjs --workspace-root ..";
    const targetTests =
      "node --test tests/current-reviewer-target.test.mjs tests/current-reviewer-ci.test.mjs";
    addOrderingError(errors, repo, text, `node-version: "${NODE_VERSION}"`, LOCAL_CHECK_COMMANDS.worker);
    for (const [name, repository, checkoutPath] of [
      ["Check out current Server test dependency", "GoPullwise/pullwise-server", "pullwise-server"],
      ["Check out current Web test dependency", "GoPullwise/pullwise-web", "pullwise-web"],
      ["Check out current Admin test dependency", "GoPullwise/pullwise-admin", "pullwise-admin"],
    ]) {
      const step = namedStep(text, name);
      if (
        !step ||
        !step.includes("uses: actions/checkout@v4") ||
        !step.includes(`repository: ${repository}`) ||
        !step.includes(`path: ${checkoutPath}`) ||
        !step.includes("persist-credentials: false")
      ) {
        errors.push(`worker:checkout:${checkoutPath}`);
      } else {
        addOrderingError(errors, repo, text, step, workspaceCheck);
      }
    }
    addOrderingError(errors, repo, text, LOCAL_CHECK_COMMANDS.worker, workspaceCheck);
    addOrderingError(errors, repo, text, workspaceCheck, targetTests);
    addOrderingError(errors, repo, text, targetTests, "actions/setup-python@v5");
    if (text.includes("check_current_reviewer_authority.py")) {
      errors.push("worker:python_target_governance");
    }
    if (text.split(HISTORICAL_LABEL).length - 1 < 6) {
      errors.push("worker:historical_cleanup_label");
    }
  }
  return [...new Set(errors)].sort();
}

function validateWorkflows(values) {
  return Object.entries(values)
    .flatMap(([repo, text]) => validateWorkflow(repo, text))
    .sort();
}

test("live workflows satisfy the exact Node.js/Pi target CI contract", () => {
  assert.deepEqual(validateWorkflows(workflows()), []);
});

test("all local checkers bind both immutable governance blocks", () => {
  for (const [repo, relativePath] of Object.entries(CHECKER_PATHS)) {
    const checker = read(relativePath);
    assert.ok(checker.includes(CURRENT_BLOCK_SHA256), `${repo} current block digest`);
    assert.ok(checker.includes(TARGET_BLOCK_SHA256), `${repo} target block digest`);
  }
});

for (const [name, mutate, expected] of [
  [
    "pinned Node version",
    (values) => ({ ...values, admin: values.admin.replace(NODE_VERSION, "22.12.0") }),
    "admin:node_version",
  ],
  [
    "checker ordering",
    (values) => ({
      ...values,
      web: values.web
        .replace(LOCAL_CHECK_COMMANDS.web, "__CHECKER__")
        .replace(FIRST_INSTALL_COMMANDS.web, LOCAL_CHECK_COMMANDS.web)
        .replace("__CHECKER__", FIRST_INSTALL_COMMANDS.web),
    }),
    `web:order:${LOCAL_CHECK_COMMANDS.web}`,
  ],
  [
    "checkout path",
    (values) => ({
      ...values,
      worker: values.worker.replace("path: pullwise-server", "path: server"),
    }),
    "worker:checkout:pullwise-server",
  ],
  [
    "read-only permissions",
    (values) => ({
      ...values,
      server: values.server.replace("contents: read", "contents: write"),
    }),
    "server:permissions_not_read_only",
  ],
  [
    "forbidden publishing",
    (values) => ({ ...values, web: `${values.web}\n      - run: npm publish\n` }),
    "web:forbidden:npm publish",
  ],
]) {
  test(`rejects tampered ${name}`, () => {
    assert.ok(validateWorkflows(mutate(workflows())).includes(expected));
  });
}

test("removes Python target-governance files after the Node replacements pass", () => {
  for (const relativePath of LEGACY_PYTHON_TARGET_PATHS) {
    assert.equal(fs.existsSync(path.join(WORKSPACE_ROOT, relativePath)), false, relativePath);
  }
});
