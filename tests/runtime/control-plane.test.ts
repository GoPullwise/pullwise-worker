import assert from "node:assert/strict";
import test from "node:test";

import { ControlPlaneClient, exchangeWorkerBootstrap } from "../../src/runtime/control-plane.ts";

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

test("model profile pull uses only the Worker control-plane credential", async () => {
  let call: { url: string; init: RequestInit; body: any } | undefined;
  const responsePayload = {
    schema_id: "pullwise-worker-model-profile/v1",
    worker_id: "wk_pi",
  };
  const client = new ControlPlaneClient({
    serverUrl: "https://api.example.com",
    workerId: "wk_pi",
    token: "worker-control-token",
    workerVersion: "0.10.24",
    hostname: "worker-host",
    fetchImpl: async (url, init = {}) => {
      call = { url: String(url), init, body: JSON.parse(String(init.body)) };
      return new Response(JSON.stringify(responsePayload), { status: 200 });
    },
  });

  const response = await client.fetchModelProfile();

  assert.deepEqual(response, responsePayload);
  assert.equal(call?.url, "https://api.example.com/v1/workers/wk_pi/model-profile");
  assert.deepEqual(call?.body, {
    schema_id: "pullwise-worker-model-profile-request/v1",
    worker_id: "wk_pi",
  });
  assert.equal(
    (call?.init.headers as Record<string, string>).Authorization,
    "Bearer worker-control-token",
  );
  assert.doesNotMatch(JSON.stringify(call?.body), /worker-control-token/u);
});

test("manifest trust pull uses the authenticated Worker bootstrap channel", async () => {
  let call: { url: string; init: RequestInit; body: any } | undefined;
  const trust = {
    schema_id: "pullwise-model-gateway-manifest-trust/v1",
    alg: "Ed25519",
    kid: "gateway-signing-2026-09",
    public_key_pem: "-----BEGIN PUBLIC KEY-----\npublic\n-----END PUBLIC KEY-----\n",
    fingerprint: "sha256:" + "a".repeat(64),
  };
  const client = new ControlPlaneClient({
    serverUrl: "https://api.example.com",
    workerId: "wk_pi",
    token: "worker-control-token",
    workerVersion: "0.10.24",
    hostname: "worker-host",
    fetchImpl: async (url, init = {}) => {
      call = { url: String(url), init, body: JSON.parse(String(init.body)) };
      return new Response(JSON.stringify(trust), { status: 200 });
    },
  });

  assert.deepEqual(await client.fetchModelProfileTrust(), trust);
  assert.equal(call?.url, "https://api.example.com/v1/workers/wk_pi/model-profile-trust");
  assert.deepEqual(call?.body, {
    schema_id: "pullwise-model-gateway-manifest-trust-request/v1",
    worker_id: "wk_pi",
  });
  assert.equal((call?.init.headers as Record<string, string>).Authorization, "Bearer worker-control-token");
});

test("single-use bootstrap exchange keeps the credential out of URL and body", async () => {
  let call: { url: string; init: RequestInit; body: any } | undefined;
  const token = await exchangeWorkerBootstrap({
    serverUrl: "https://api.example.com",
    workerId: "wk_batch_1",
    bootstrapToken: "pwb_single-use-secret",
    fetchImpl: async (url, init = {}) => {
      call = { url: String(url), init, body: JSON.parse(String(init.body)) };
      return new Response(JSON.stringify({
        worker_id: "wk_batch_1",
        worker_token: "pww_control-token",
      }), { status: 200 });
    },
  });

  assert.equal(token, "pww_control-token");
  assert.equal(call?.url, "https://api.example.com/v1/workers/bootstrap");
  assert.deepEqual(call?.body, {
    schema_id: "pullwise-worker-bootstrap-exchange/v1",
    worker_id: "wk_batch_1",
  });
  assert.equal((call?.init.headers as Record<string, string>).Authorization, "Bearer pwb_single-use-secret");
  assert.doesNotMatch(call?.url ?? "", /single-use-secret/u);
  assert.doesNotMatch(JSON.stringify(call?.body), /single-use-secret/u);
});

test("registration and heartbeat report de-secreted desired and applied profile state", async () => {
  const bodies: any[] = [];
  const client = new ControlPlaneClient({
    serverUrl: "https://api.example.com",
    workerId: "wk_pi",
    token: "worker-control-token",
    workerVersion: "0.10.24",
    hostname: "worker-host",
    fetchImpl: async (_url, init = {}) => {
      bodies.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    },
  });
  const profileState = {
    schemaId: "pullwise-worker-profile-state/v1" as const,
    workerId: "wk_pi",
    workerPoolId: "reviewers-primary",
    profileSetId: "reviewer-production",
    desiredRevision: 12,
    appliedRevision: 12,
    manifestDigest: "a".repeat(64),
    catalogDigest: "b".repeat(64),
    gatewayTokenExpiresAt: 1_788_259_500,
    gatewayTokenId: "gtj_wk_pi",
    lastApplyResult: "succeeded" as const,
    appliedAt: 1_788_259_201,
  };

  await client.register(catalog, undefined, profileState);
  await client.heartbeat(catalog, undefined, undefined, profileState);

  const expected = {
    schema_id: "pullwise-worker-profile-state/v1",
    worker_id: "wk_pi",
    worker_pool_id: "reviewers-primary",
    profile_set_id: "reviewer-production",
    desired_revision: 12,
    applied_revision: 12,
    manifest_digest: "a".repeat(64),
    catalog_digest: "b".repeat(64),
    gateway_token_expires_at: 1_788_259_500,
    gateway_token_id: "gtj_wk_pi",
    last_apply_result: "succeeded",
    applied_at: 1_788_259_201,
  };
  assert.deepEqual(bodies[0]?.worker.profile_state, expected);
  assert.deepEqual(bodies[1]?.profile_state, expected);
  assert.doesNotMatch(JSON.stringify(bodies), /access_token|worker-control-token/u);
});
