import assert from "node:assert/strict";
import test from "node:test";

import { ControlPlaneClient } from "../../src/runtime/control-plane.ts";

const catalog = {
  schema_id: "pullwise-pi-runtime-catalog/v1",
  credentials: [{
    credential_id: "openai_team",
    label: "OpenAI team",
    provider: "openai",
    auth_type: "api_key",
    models: [{ id: "gpt-5.1", name: "GPT-5.1" }],
  }],
} as const;

test("control-plane registration and heartbeat reuse v1 routes with de-secreted catalog", async () => {
  const calls: Array<{ url: string; init: RequestInit; body: any }> = [];
  const fetchImpl = async (url: string | URL | Request, init: RequestInit = {}) => {
    calls.push({ url: String(url), init, body: JSON.parse(String(init.body)) });
    return new Response(JSON.stringify({ ok: true, accepted: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  const client = new ControlPlaneClient({
    serverUrl: "https://api.example.com",
    workerId: "wk_pi",
    token: "worker-secret",
    workerVersion: "0.10.24",
    hostname: "worker-host",
    fetchImpl,
  });

  await client.register(catalog);
  await client.heartbeat(catalog);

  assert.deepEqual(calls.map((call) => call.url), [
    "https://api.example.com/v1/workers/register",
    "https://api.example.com/v1/workers/wk_pi/heartbeat",
  ]);
  assert.equal(calls[0]?.body.worker.runtime_catalog.schema_id, catalog.schema_id);
  assert.equal(calls[1]?.body.runtime_catalog.credentials[0].provider, "openai");
  assert.deepEqual(calls[1]?.body.agent_session, {
    status: "idle",
    transport: "embedded",
    active_session_id: null,
  });
  assert.equal((calls[0]?.init.headers as Record<string, string>).Authorization, "Bearer worker-secret");
  assert.doesNotMatch(JSON.stringify(calls.map((call) => call.body)), /worker-secret/u);
});

test("busy heartbeat carries the Watcher-observed active Pi session state", async () => {
  let body: any;
  const client = new ControlPlaneClient({
    serverUrl: "https://api.example.com",
    workerId: "wk_pi",
    token: "worker-secret",
    workerVersion: "0.10.24",
    hostname: "worker-host",
    fetchImpl: async (_url, init = {}) => {
      body = JSON.parse(String(init.body));
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    },
  });

  await client.heartbeat(catalog, {
    status: "busy",
    activeRunId: "run_1",
    activeSessionId: "pi_session_1",
    progress: {
      run_id: "run_1",
      overall_percent: 25,
      current_phase: "review",
      current_phase_status: "running",
      current_phase_percent: 25,
      message: "Reviewing",
      counters: {},
      active_unit: {},
      last_event_sequence: 1,
      updated_at: "2026-09-01T00:00:00Z",
    },
  });

  assert.equal(body.status, "busy");
  assert.equal(body.active_run_id, "run_1");
  assert.equal(body.concurrency.active_jobs, 1);
  assert.deepEqual(body.agent_session, {
    status: "running",
    transport: "embedded",
    active_session_id: "pi_session_1",
  });
  assert.equal(body.progress.run_id, "run_1");
});

test("lease, artifact, and result calls stay on the existing v1 routes", async () => {
  const calls: Array<{ url: string; body: any }> = [];
  const client = new ControlPlaneClient({
    serverUrl: "https://api.example.com",
    workerId: "wk_pi",
    token: "worker-secret",
    workerVersion: "0.10.24",
    hostname: "worker-host",
    fetchImpl: async (url, init = {}) => {
      calls.push({ url: String(url), body: JSON.parse(String(init.body)) });
      const payload = String(url).endsWith("/lease")
        ? { lease: { run_id: "run_1" }, job: { job_id: "job_1" } }
        : { ok: true };
      return new Response(JSON.stringify(payload), { status: 200 });
    },
  });

  const lease = await client.lease();
  await client.uploadArtifact("run_1", { artifact_id: "artifact_1" });
  await client.submitResult("run_1", { protocol_version: "review-worker-protocol/v1" });

  assert.deepEqual(lease, { lease: { run_id: "run_1" }, job: { job_id: "job_1" } });
  assert.deepEqual(calls.map((call) => call.url), [
    "https://api.example.com/v1/workers/wk_pi/lease",
    "https://api.example.com/v1/review-runs/run_1/artifacts",
    "https://api.example.com/v1/review-runs/run_1/result",
  ]);
  assert.equal(calls[0]?.body.capabilities.pi_agent_session, true);
});

test("local degraded state maps to an idle slot with not-ready diagnostics", async () => {
  let body: any;
  const client = new ControlPlaneClient({
    serverUrl: "https://api.example.com",
    workerId: "wk_pi",
    token: "worker-secret",
    workerVersion: "0.10.24",
    hostname: "worker-host",
    fetchImpl: async (_url, init = {}) => {
      body = JSON.parse(String(init.body));
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    },
  });
  await client.heartbeat(catalog, {
    status: "degraded",
    activeRunId: null,
    activeSessionId: null,
    progress: null,
    lastError: "worker_state_unavailable",
  });
  assert.equal(body.status, "idle");
  assert.equal(body.doctor_status, "not_ready");
  assert.equal(body.concurrency.active_jobs, 0);
  assert.equal(body.last_error, "worker_state_unavailable");
});
