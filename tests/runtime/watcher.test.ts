import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { addProfile } from "../../src/runtime/profiles.ts";
import { syncWatcherOnce } from "../../src/runtime/watcher.ts";
import { writeWorkerState } from "../../src/runtime/worker-state.ts";

test("Watcher is the sole bridge from local Worker state to Server heartbeat", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-watcher-"));
  const profileRoot = join(root, "profiles");
  const stateRoot = join(root, "state");
  try {
    await addProfile(profileRoot, {
      credentialId: "openai_team",
      label: "OpenAI team",
      provider: "openai",
    });
    await writeWorkerState(stateRoot, {
      status: "busy",
      activeRunId: "run_1",
      activeSessionId: "session_1",
      lastError: null,
      progress: { run_id: "run_1", overall_percent: 10 },
    });
    const calls: any[] = [];
    const commands: any[] = [];
    const client = {
      register: async (catalog: unknown) => calls.push(["register", catalog]),
      heartbeat: async (catalog: unknown, state: unknown) => {
        calls.push(["heartbeat", catalog, state]);
        return { commands: [{ type: "cancel_run", run_id: "run_1", reason: "user" }] };
      },
    };

    await syncWatcherOnce({
      profileRoot,
      stateRoot,
      client,
      register: true,
      listModels: async () => [{ id: "gpt-5.1", name: "GPT-5.1" }],
      handleCommand: async (command) => { commands.push(command); },
    });

    assert.equal(calls[0]?.[0], "register");
    assert.equal(calls[1]?.[0], "heartbeat");
    assert.equal(calls[1]?.[2].status, "busy");
    assert.equal(calls[1]?.[2].activeRunId, "run_1");
    assert.equal(commands[0]?.type, "cancel_run");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("missing execution state is reported as degraded, never idle", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-watcher-missing-"));
  try {
    const states: any[] = [];
    await syncWatcherOnce({
      profileRoot: join(root, "profiles"),
      stateRoot: join(root, "state"),
      client: {
        register: async () => {},
        heartbeat: async (_catalog: unknown, state: unknown) => {
          states.push(state);
          return {};
        },
      },
      register: false,
      listModels: async () => [],
    });
    assert.equal(states[0].status, "degraded");
    assert.match(states[0].lastError, /worker_state_unavailable/u);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
