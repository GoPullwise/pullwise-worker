import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { materializeCheckout } from "../../src/runtime/checkout.ts";

test("checkout keeps clone token out of argv and creates an attempt-owned workspace", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-checkout-"));
  try {
    const calls: any[] = [];
    const checkout = await materializeCheckout({
      job_id: "job_1",
      run_id: "run_1",
      repository: {
        clone_url: "https://github.com/acme/api.git",
        commit_sha: "abcdef1234567890",
      },
      clone_token: { token: "clone-secret" },
    }, {
      checkoutRoot: root,
      runCommand: async (_command, args, options) => {
        calls.push({ args, env: options.env });
        const destination = args.at(-1);
        if (args[0] === "clone") {
          assert.ok(destination);
          await mkdir(destination, { recursive: true });
        }
      },
    });

    assert.equal(calls.length, 2);
    assert.doesNotMatch(JSON.stringify(calls.map((call) => call.args)), /clone-secret/u);
    assert.match(calls[0].env.GIT_CONFIG_VALUE_0, /clone-secret/u);
    assert.equal(calls[1].args[0], "-C");
    assert.match(checkout.workspace, /job_1/u);
    await checkout.cleanup();
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

for (const [name, job] of [
  ["non-GitHub URL", {
    job_id: "job_1", run_id: "run_1",
    repository: { clone_url: "https://example.com/acme/api.git", commit_sha: "abcdef1234567890" },
    clone_token: { token: "secret" },
  }],
  ["invalid commit", {
    job_id: "job_1", run_id: "run_1",
    repository: { clone_url: "https://github.com/acme/api.git", commit_sha: "main" },
    clone_token: { token: "secret" },
  }],
] as const) {
  test(`checkout rejects ${name} before command execution`, async () => {
    const root = await mkdtemp(join(tmpdir(), "pullwise-checkout-invalid-"));
    let calls = 0;
    try {
      await assert.rejects(materializeCheckout(job, {
        checkoutRoot: root,
        runCommand: async () => { calls += 1; },
      }));
      assert.equal(calls, 0);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
}
