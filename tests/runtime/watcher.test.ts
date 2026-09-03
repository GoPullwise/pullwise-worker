import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { syncWatcherOnce } from "../../src/runtime/watcher.ts";
import { writeWorkerState } from "../../src/runtime/worker-state.ts";

const PROFILE_DIGEST = "a".repeat(64);

async function installManagedProfile(root: string): Promise<void> {
  const generation = `${PROFILE_DIGEST}.gtj_watcher`;
  const generationRoot = join(root, "generations", generation);
  await mkdir(join(generationRoot, "profiles", "gateway-reviewer-production"), { recursive: true });
  await writeFile(join(root, "managed-current.json"), JSON.stringify({
    schema_id: "pullwise-managed-profile-pointer/v1",
    generation,
    manifest_digest: PROFILE_DIGEST,
  }));
  await writeFile(join(generationRoot, "profiles.json"), JSON.stringify({
    schema_id: "pullwise-pi-profiles/v1",
    profiles: [{
      credential_id: "gateway-reviewer-production",
      label: "Reviewer production",
      provider: "pullwise-gateway",
      auth_type: "api_key",
      agent_dir: "profiles/gateway-reviewer-production",
    }],
  }));
}

test("Watcher is the sole bridge from local Worker state to Server heartbeat", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-watcher-"));
  const profileRoot = join(root, "profiles");
  const stateRoot = join(root, "state");
  try {
    await installManagedProfile(profileRoot);
    await writeWorkerState(stateRoot, {
      status: "busy",
      activeRunId: "run_1",
      activeSessionId: "session_1",
      lastError: null,
      progress: { run_id: "run_1", overall_percent: 10 },
    });
    const calls: any[] = [];
    const commands: any[] = [];
    const order: string[] = [];
    const profileState = {
      schemaId: "pullwise-worker-profile-state/v1" as const,
      workerId: "wk_pi",
      workerPoolId: "reviewers-primary",
      profileSetId: "reviewer-production",
      desiredRevision: 12,
      appliedRevision: 12,
      manifestDigest: PROFILE_DIGEST,
      catalogDigest: "b".repeat(64),
      gatewayTokenExpiresAt: 1_788_259_500,
      gatewayTokenId: "gtj_wk_pi",
      lastApplyResult: "succeeded" as const,
      appliedAt: 1_788_259_201,
    };
    const client = {
      register: async (catalog: unknown, _signal?: AbortSignal, observed?: unknown) => {
        order.push("register");
        calls.push(["register", catalog, observed]);
      },
      heartbeat: async (catalog: unknown, state: unknown, _signal?: AbortSignal, observed?: unknown) => {
        order.push("heartbeat");
        calls.push(["heartbeat", catalog, state, observed]);
        return { commands: [{ type: "cancel_run", run_id: "run_1", reason: "user" }] };
      },
    };

    await syncWatcherOnce({
      profileRoot,
      stateRoot,
      client,
      register: true,
      reconcileProfile: async () => {
        order.push("reconcile");
        return profileState;
      },
      listModels: async () => {
        order.push("catalog");
        return [{ id: "gpt-5.1", name: "GPT-5.1" }];
      },
      handleCommand: async (command) => { commands.push(command); },
    });

    assert.deepEqual(order, ["reconcile", "catalog", "register", "heartbeat"]);
    assert.equal(calls[0]?.[0], "register");
    assert.deepEqual(calls[0]?.[2], profileState);
    assert.equal(calls[1]?.[0], "heartbeat");
    assert.equal(calls[1]?.[2].status, "busy");
    assert.equal(calls[1]?.[2].activeRunId, "run_1");
    assert.deepEqual(calls[1]?.[3], profileState);
    assert.equal(commands[0]?.type, "cancel_run");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("missing execution state is reported as degraded, never idle", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-watcher-missing-"));
  try {
    await installManagedProfile(join(root, "profiles"));
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
