import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { readWorkerState, writeWorkerState } from "../../src/runtime/worker-state.ts";

test("Worker state round-trips through one atomic closed document", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-worker-state-"));
  try {
    await writeWorkerState(root, {
      status: "busy",
      activeRunId: "run_1",
      activeSessionId: "session_1",
      lastError: null,
      progress: { run_id: "run_1", overall_percent: 10 },
    });
    const state = await readWorkerState(root);
    assert.equal(state.status, "busy");
    assert.equal(state.activeRunId, "run_1");
    assert.equal(state.activeSessionId, "session_1");
    assert.deepEqual(state.progress, { run_id: "run_1", overall_percent: 10 });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Worker state rejects unknown fields and stale active bindings", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-worker-state-invalid-"));
  try {
    await writeFile(
      join(root, "worker-state.json"),
      JSON.stringify({
        schema_id: "pullwise-worker-state/v1",
        status: "idle",
        active_run_id: "run_stale",
        active_session_id: null,
        last_error: null,
        progress: null,
        updated_at: "2026-09-01T00:00:00Z",
        secret: "forbidden",
      }),
      "utf8",
    );
    await assert.rejects(readWorkerState(root), /closed object/u);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
