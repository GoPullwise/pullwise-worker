import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  CURRENT_END,
  CURRENT_START,
  REPOSITORIES,
  TARGET_BLOCK,
  validateWorkspace,
} from "../scripts/check-current-reviewer-target.mjs";

const WORKSPACE_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
);
const AUTHORITY_BLOCK = `${CURRENT_START}
current authority
${CURRENT_END}`;

function fixtureWorkspace() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pullwise-r0-pi-gov-"));
  for (const directory of Object.values(REPOSITORIES)) {
    fs.mkdirSync(path.join(root, directory), { recursive: true });
    fs.writeFileSync(
      path.join(root, directory, "AGENTS.md"),
      AUTHORITY_BLOCK + "\n" + TARGET_BLOCK + "\n# Repository rules\n",
      "utf8",
    );
  }
  return root;
}

function withWorkspace(callback) {
  const root = fixtureWorkspace();
  try {
    callback(root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

function overwriteWorker(root, text) {
  fs.writeFileSync(path.join(root, REPOSITORIES.worker, "AGENTS.md"), text, "utf8");
}

test("accepts the exact four-repository Node.js and Pi target", () => {
  withWorkspace((root) => {
    assert.equal(validateWorkspace(root).status, "PASS");
  });
});

for (const [name, mutation] of [
  ["Codex target", (block) => block.replace("sole Worker target", "Codex Worker target")],
  ["Python Worker", (block) => block.replace("Node.js/TypeScript", "Python")],
  ["quota-window readiness", (block) =>
    block.replace("Do not query or poll", "Query and poll")],
]) {
  test(`rejects stale ${name} governance`, () => {
    withWorkspace((root) => {
      overwriteWorker(
        root,
        AUTHORITY_BLOCK + "\n" + mutation(TARGET_BLOCK) + "\n",
      );
      const report = validateWorkspace(root);
      assert.equal(report.status, "FAIL");
      assert.ok(
        report.repositories.find((item) => item.repository === "worker")
          .errors.includes("target_block_mismatch"),
      );
    });
  });
}

test("preserves unrelated product scan-quota guidance", () => {
  withWorkspace((root) => {
    overwriteWorker(
      root,
      AUTHORITY_BLOCK + "\n" + TARGET_BLOCK +
        "\nAccount and repository scan quotas remain product controls.\n",
    );
    assert.equal(validateWorkspace(root).status, "PASS");
  });
});

test("fails closed when a repository instruction file is missing", () => {
  withWorkspace((root) => {
    fs.rmSync(path.join(root, REPOSITORIES.web, "AGENTS.md"));
    assert.equal(validateWorkspace(root).status, "INDETERMINATE");
  });
});

test("fails closed for non-UTF-8 instruction bytes", () => {
  withWorkspace((root) => {
    fs.writeFileSync(
      path.join(root, REPOSITORIES.server, "AGENTS.md"),
      Buffer.from([0xff, 0xfe, 0xfd]),
    );
    assert.equal(validateWorkspace(root).status, "INDETERMINATE");
  });
});

test("fails closed for a symlinked instruction file", () => {
  withWorkspace((root) => {
    const target = path.join(root, "outside-agents.md");
    fs.writeFileSync(target, AUTHORITY_BLOCK + "\n" + TARGET_BLOCK + "\n", "utf8");
    const agents = path.join(root, REPOSITORIES.admin, "AGENTS.md");
    fs.rmSync(agents);
    fs.symlinkSync(target, agents);
    assert.equal(validateWorkspace(root).status, "INDETERMINATE");
  });
});

test("the real workspace carries the exact target block", () => {
  const report = validateWorkspace(WORKSPACE_ROOT);
  assert.equal(report.status, "PASS", JSON.stringify(report));
});
