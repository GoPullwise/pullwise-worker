import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { executeLeaseJob } from "../../src/runtime/lease-job.ts";

test("leased job uses the exact profile once and publishes five v1 artifacts plus result", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-lease-job-"));
  try {
    const states: any[] = [];
    const uploads: any[] = [];
    const reviews: any[] = [];
    let cleaned = false;
    const result = await executeLeaseJob({
      workerId: "wk_pi",
      workerVersion: "0.10.24",
      stateRoot: join(root, "state"),
      profiles: {
        root: join(root, "profiles"),
        profiles: [{
          credentialId: "openai_team",
          label: "OpenAI team",
          provider: "openai",
          authType: "api_key",
          agentDir: join(root, "profiles", "openai_team"),
        }],
      },
      job: {
        job_id: "job_1",
        run_id: "run_1",
        lease_id: "lease_1",
        attempt: 1,
        runtime_selection: {
          credential_id: "openai_team",
          provider: "openai",
          model: "gpt-5.1",
        },
        repository: {
          clone_url: "https://github.com/acme/api.git",
          commit_sha: "abcdef1234567890",
        },
        clone_token: { token: "clone-secret" },
        review_request: {
          budget: { max_wall_time_seconds: 60, max_estimated_input_tokens: 1000 },
        },
      },
      materialize: async () => ({
        workspace: join(root, "checkout"),
        cleanup: async () => { cleaned = true; },
      }),
      review: async (attempt, profile, options) => {
        reviews.push({ attempt, profile });
        await options.onSessionStarted?.({
          sessionId: "pi_session_1",
          model: { provider: "openai", id: "gpt-5.1" },
        });
        return {
          attemptId: attempt.attemptId,
          sessionId: "pi_session_1",
          model: { provider: "openai", id: "gpt-5.1" },
          usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, total: 15, cost: 0.01 },
          startedAt: 1,
          finishedAt: 2,
          payload: { summary: "No findings.", findings: [], coverage: [] },
        };
      },
      client: {
        uploadArtifact: async (_runId, payload) => uploads.push(payload),
        submitResult: async (_runId, payload) => ({ accepted: true, payload }),
      },
      writeState: async (state) => {
        states.push(state);
      },
    });

    assert.equal(reviews.length, 1);
    assert.equal(reviews[0].profile.credentialId, "openai_team");
    assert.equal(reviews[0].attempt.provider, "openai");
    assert.equal(reviews[0].attempt.model, "gpt-5.1");
    assert.doesNotMatch(JSON.stringify(reviews[0].attempt.context), /clone-secret/u);
    assert.equal(uploads.length, 5);
    assert.equal(result.accepted, true);
    assert.deepEqual(states.map((state) => state.status), ["busy", "busy", "finishing", "idle"]);
    assert.equal(states[1].activeSessionId, "pi_session_1");
    assert.equal(cleaned, true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("lease selection must match one local credential profile exactly", async () => {
  await assert.rejects(
    executeLeaseJob({
      workerId: "wk_pi",
      workerVersion: "0.10.24",
      stateRoot: ".",
      profiles: { root: ".", profiles: [] },
      job: {
        job_id: "job_1",
        run_id: "run_1",
        lease_id: "lease_1",
        attempt: 1,
        runtime_selection: { credential_id: "missing", provider: "openai", model: "gpt-5.1" },
      },
      materialize: async () => { throw new Error("must not materialize"); },
      review: async () => { throw new Error("must not review"); },
      client: {
        uploadArtifact: async () => {},
        submitResult: async () => ({}),
      },
      writeState: async () => {},
    }),
    /does not match a local profile/u,
  );
});

test("cancelled execution publishes the required terminal diagnostics", async () => {
  const controller = new AbortController();
  controller.abort();
  const uploads: any[] = [];
  const results: any[] = [];
  const states: any[] = [];
  const response = await executeLeaseJob({
    workerId: "wk_pi",
    workerVersion: "0.10.24",
    stateRoot: ".",
    profiles: {
      root: ".",
      profiles: [{
        credentialId: "openai_team",
        label: "OpenAI team",
        provider: "openai",
        authType: "api_key",
        agentDir: ".",
      }],
    },
    job: {
      job_id: "job_1",
      run_id: "run_1",
      lease_id: "lease_1",
      attempt: 1,
      runtime_selection: { credential_id: "openai_team", provider: "openai", model: "gpt-5.1" },
    },
    materialize: async () => ({ workspace: ".", cleanup: async () => {} }),
    review: async () => { throw new Error("cancelled"); },
    client: {
      uploadArtifact: async (_runId, payload) => { uploads.push(payload); },
      submitResult: async (_runId, payload) => {
        results.push(payload);
        return { accepted: true };
      },
    },
    writeState: async (state) => { states.push(state); },
    signal: controller.signal,
  });
  assert.equal(response.accepted, true);
  assert.equal(uploads.length, 3);
  assert.equal(results[0].status, "cancelled");
  assert.equal(states.at(-1).status, "idle");
});
