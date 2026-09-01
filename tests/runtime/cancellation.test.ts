import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  cancellationRequested,
  requestCancellation,
} from "../../src/runtime/cancellation.ts";

test("Watcher cancellation marker is run-scoped and contains no arbitrary fields", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-cancel-"));
  try {
    assert.equal(await cancellationRequested(root, "run_1"), false);
    await requestCancellation(root, "run_1", "user_request");
    assert.equal(await cancellationRequested(root, "run_1"), true);
    assert.equal(await cancellationRequested(root, "run_2"), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
