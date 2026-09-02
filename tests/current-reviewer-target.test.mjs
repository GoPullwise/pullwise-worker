import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  REPOSITORIES,
  TARGET_BLOCK,
  validateRepository,
  validateWorkspace,
} from "../scripts/check-current-reviewer-target.mjs";

const WORKER_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const WORKSPACE_ROOT = path.resolve(
  WORKER_ROOT,
  "..",
);

function fixtureWorkspace() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pullwise-r0-pi-gov-"));
  for (const directory of Object.values(REPOSITORIES)) {
    fs.mkdirSync(path.join(root, directory), { recursive: true });
    fs.writeFileSync(
      path.join(root, directory, "AGENTS.md"),
      TARGET_BLOCK + "\n# Repository rules\n",
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

function withNativeRealpathOverride(target, replacement, callback) {
  const original = fs.realpathSync.native;
  fs.realpathSync.native = (candidate, options) =>
    path.resolve(candidate) === path.resolve(target)
      ? path.resolve(replacement)
      : original(candidate, options);
  try {
    callback();
  } finally {
    fs.realpathSync.native = original;
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

test("accepts an exact single-repository Worker target", () => {
  withWorkspace((root) => {
    const report = validateRepository(root, "worker");
    assert.equal(report.status, "PASS", JSON.stringify(report));
    assert.deepEqual(report.repositories.map((item) => item.repository), ["worker"]);
  });
});

test("rejects instructions placed before the local target block", () => {
  withWorkspace((root) => {
    overwriteWorker(
      root,
      "# External authority\n" + TARGET_BLOCK + "\n",
    );
    const report = validateWorkspace(root);
    assert.equal(report.status, "FAIL");
    assert.ok(
      report.repositories.find((item) => item.repository === "worker")
        .errors.includes("target_block_not_first"),
    );
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
        mutation(TARGET_BLOCK) + "\n",
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
      TARGET_BLOCK + "\nAccount and repository scan quotas remain product controls.\n",
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

test("fails closed for a symlinked instruction file", (context) => {
  withWorkspace((root) => {
    const target = path.join(root, "outside-agents.md");
    fs.writeFileSync(target, TARGET_BLOCK + "\n", "utf8");
    const agents = path.join(root, REPOSITORIES.admin, "AGENTS.md");
    fs.rmSync(agents);
    try {
      fs.symlinkSync(target, agents);
    } catch (error) {
      if (error?.code === "EPERM") {
        context.skip("file symlinks are unavailable in this Windows environment");
        return;
      }
      throw error;
    }
    assert.equal(validateWorkspace(root).status, "INDETERMINATE");
  });
});

test("fails closed for a linked repository root", () => {
  withWorkspace((root) => {
    const repository = path.join(root, REPOSITORIES.admin);
    const target = path.join(root, "linked-admin-target");
    fs.renameSync(repository, target);
    fs.symlinkSync(target, repository, "junction");

    const report = validateWorkspace(root);
    const admin = report.repositories.find((item) => item.repository === "admin");
    assert.equal(report.status, "INDETERMINATE");
    assert.ok(admin.errors.includes("repository_path_not_safe"), JSON.stringify(admin));
  });
});

test("fails closed when a repository reparse target resolves outside the workspace", () => {
  withWorkspace((root) => {
    const repository = path.join(root, REPOSITORIES.server);
    const outside = path.join(path.dirname(root), "outside-server");

    withNativeRealpathOverride(repository, outside, () => {
      const report = validateWorkspace(root);
      const server = report.repositories.find((item) => item.repository === "server");
      assert.equal(report.status, "INDETERMINATE");
      assert.ok(
        server.errors.includes("repository_path_outside_workspace"),
        JSON.stringify(server),
      );
    });
  });
});

test("fails closed when AGENTS.md resolves outside its repository", () => {
  withWorkspace((root) => {
    const agents = path.join(root, REPOSITORIES.web, "AGENTS.md");
    const outside = path.join(path.dirname(root), "outside-agents.md");

    withNativeRealpathOverride(agents, outside, () => {
      const report = validateWorkspace(root);
      const web = report.repositories.find((item) => item.repository === "web");
      assert.equal(report.status, "INDETERMINATE");
      assert.ok(
        web.errors.includes("agents_file_outside_repository"),
        JSON.stringify(web),
      );
    });
  });
});

test("the real Worker carries the exact target block", () => {
  const report = validateRepository(WORKSPACE_ROOT, "worker");
  assert.equal(report.status, "PASS", JSON.stringify(report));
});
