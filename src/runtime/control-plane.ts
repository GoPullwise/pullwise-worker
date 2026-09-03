import type { GatewayProfileState } from "./gateway-profile.ts";

export interface RuntimeCatalog {
  readonly schema_id: "pullwise-pi-runtime-catalog/v1";
  readonly credentials: readonly {
    readonly credential_id: string;
    readonly label: string;
    readonly provider: string;
    readonly auth_type: string;
    readonly models: readonly { readonly id: string; readonly name: string }[];
  }[];
}

export interface ControlPlaneOptions {
  readonly serverUrl: string;
  readonly workerId: string;
  readonly token: string;
  readonly workerVersion: string;
  readonly hostname: string;
  readonly fetchImpl?: typeof fetch;
}

export interface HeartbeatState {
  readonly status: "idle" | "busy" | "degraded" | "cancelling" | "finishing";
  readonly activeRunId: string | null;
  readonly activeSessionId: string | null;
  readonly progress?: Readonly<Record<string, unknown>> | null;
  readonly lastError?: string | null;
}

const IDLE_STATE: HeartbeatState = Object.freeze({
  status: "idle",
  activeRunId: null,
  activeSessionId: null,
  progress: null,
  lastError: null,
});

const PROGRESS_COUNTERS = [
  "source_like_files_total",
  "source_like_files_classified",
  "bundles_total",
  "bundles_packed",
  "reviewer_runs_total",
  "reviewer_runs_completed",
  "intent_tests_total",
  "intent_tests_written",
  "intent_tests_run",
  "validator_candidates_total",
  "validator_candidates_completed",
  "artifacts_total",
  "artifacts_uploaded",
] as const;

function safeIdentifier(value: string, label: string): string {
  const text = String(value ?? "").trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u.test(text)) {
    throw new TypeError(`${label} is invalid`);
  }
  return text;
}

function providers(catalog: RuntimeCatalog): string[] {
  return [...new Set(catalog.credentials.map((credential) => credential.provider))];
}

function gatewayProfileState(state: GatewayProfileState) {
  return {
    schema_id: state.schemaId,
    worker_id: state.workerId,
    worker_pool_id: state.workerPoolId,
    profile_set_id: state.profileSetId,
    desired_revision: state.desiredRevision,
    applied_revision: state.appliedRevision,
    manifest_digest: state.manifestDigest,
    catalog_digest: state.catalogDigest,
    gateway_token_expires_at: state.gatewayTokenExpiresAt,
    gateway_token_id: state.gatewayTokenId,
    last_apply_result: state.lastApplyResult,
    applied_at: state.appliedAt,
  };
}

export interface BootstrapExchangeOptions {
  readonly serverUrl: string;
  readonly workerId: string;
  readonly bootstrapToken: string;
  readonly fetchImpl?: typeof fetch;
  readonly signal?: AbortSignal;
}

export async function exchangeWorkerBootstrap(options: BootstrapExchangeOptions): Promise<string> {
  const baseUrl = new URL(options.serverUrl);
  if (!['http:', 'https:'].includes(baseUrl.protocol) || baseUrl.username || baseUrl.password) {
    throw new TypeError("serverUrl must be an HTTP(S) URL without credentials");
  }
  const workerId = safeIdentifier(options.workerId, "workerId");
  const bootstrapToken = String(options.bootstrapToken ?? "").trim();
  if (!bootstrapToken.startsWith("pwb_") || /[\r\n]/u.test(bootstrapToken)) {
    throw new TypeError("bootstrapToken is invalid");
  }
  const init: RequestInit = {
    method: "POST",
    headers: {
      Authorization: `Bearer ${bootstrapToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      schema_id: "pullwise-worker-bootstrap-exchange/v1",
      worker_id: workerId,
    }),
  };
  if (options.signal) init.signal = options.signal;
  const response = await (options.fetchImpl ?? fetch)(new URL("/v1/workers/bootstrap", baseUrl), init);
  const text = await response.text();
  let payload: unknown;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`Pullwise Server returned invalid JSON (${response.status})`);
  }
  if (!response.ok) throw new Error("Worker bootstrap exchange failed");
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("Worker bootstrap response is invalid");
  }
  const record = payload as Record<string, unknown>;
  if (JSON.stringify(Object.keys(record).sort()) !== JSON.stringify(["worker_id", "worker_token"])) {
    throw new Error("Worker bootstrap response is invalid");
  }
  const workerToken = String(record.worker_token ?? "");
  if (record.worker_id !== workerId || !workerToken.startsWith("pww_") || /[\r\n]/u.test(workerToken)) {
    throw new Error("Worker bootstrap response is invalid");
  }
  return workerToken;
}

export class ControlPlaneClient {
  private readonly baseUrl: URL;
  private readonly workerId: string;
  private readonly token: string;
  private readonly workerVersion: string;
  private readonly hostname: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ControlPlaneOptions) {
    this.baseUrl = new URL(options.serverUrl);
    if (!['http:', 'https:'].includes(this.baseUrl.protocol) || this.baseUrl.username || this.baseUrl.password) {
      throw new TypeError("serverUrl must be an HTTP(S) URL without credentials");
    }
    this.workerId = safeIdentifier(options.workerId, "workerId");
    this.token = String(options.token ?? "").trim();
    if (!this.token) throw new TypeError("token is required");
    this.workerVersion = safeIdentifier(options.workerVersion, "workerVersion");
    this.hostname = String(options.hostname ?? "").trim().slice(0, 255);
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  private async post(pathname: string, body: object, signal?: AbortSignal): Promise<unknown> {
    const url = new URL(pathname, this.baseUrl);
    const init: RequestInit = {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    };
    if (signal) init.signal = signal;
    const response = await this.fetchImpl(url, init);
    const text = await response.text();
    let payload: unknown = {};
    if (text) {
      try {
        payload = JSON.parse(text) as unknown;
      } catch {
        throw new Error(`Pullwise Server returned invalid JSON (${response.status})`);
      }
    }
    if (!response.ok) {
      const message = payload && typeof payload === "object" && "message" in payload
        ? String((payload as { message: unknown }).message)
        : `Pullwise Server request failed (${response.status})`;
      throw new Error(message);
    }
    return payload;
  }

  register(
    catalog: RuntimeCatalog,
    signal?: AbortSignal,
    profileState?: GatewayProfileState,
  ): Promise<unknown> {
    return this.post("/v1/workers/register", {
      protocol_version: "review-worker-protocol/v1",
      worker: {
        worker_id: this.workerId,
        worker_group: "default",
        worker_version: this.workerVersion,
        hostname: this.hostname,
        concurrency: {
          max_active_jobs: 1,
          maintains_local_queue: false,
          prefetch_jobs: false,
        },
        platform: { os: "linux", arch: process.arch },
        capabilities: {
          full_repo_scan: true,
          pi_agent_session: true,
          isolated_pi_profiles: true,
          progress_events: true,
          cancellation: true,
          intent_test_validation: true,
          max_active_jobs: 1,
        },
        runtime_catalog: catalog,
        ...(profileState ? { profile_state: gatewayProfileState(profileState) } : {}),
      },
    }, signal);
  }

  fetchModelProfile(signal?: AbortSignal): Promise<unknown> {
    return this.post(`/v1/workers/${encodeURIComponent(this.workerId)}/model-profile`, {
      schema_id: "pullwise-worker-model-profile-request/v1",
      worker_id: this.workerId,
    }, signal);
  }

  fetchModelProfileTrust(signal?: AbortSignal): Promise<unknown> {
    return this.post(`/v1/workers/${encodeURIComponent(this.workerId)}/model-profile-trust`, {
      schema_id: "pullwise-model-gateway-manifest-trust-request/v1",
      worker_id: this.workerId,
    }, signal);
  }

  heartbeat(
    catalog: RuntimeCatalog,
    state: HeartbeatState = IDLE_STATE,
    signal?: AbortSignal,
    profileState?: GatewayProfileState,
  ): Promise<unknown> {
    const readyProviders = providers(catalog);
    const active = ["busy", "cancelling", "finishing"].includes(state.status);
    const wireStatus = state.status === "degraded" ? "idle" : state.status;
    const sourceProgress = state.progress ?? {};
    const sourceCounters = sourceProgress.counters && typeof sourceProgress.counters === "object"
      ? sourceProgress.counters as Record<string, unknown>
      : {};
    const counters = Object.fromEntries(
      PROGRESS_COUNTERS.map((key) => [key, Number(sourceCounters[key] ?? 0)]),
    );
    const progress = active ? {
      ...sourceProgress,
      run_id: state.activeRunId,
      counters,
      active_unit: sourceProgress.active_unit && typeof sourceProgress.active_unit === "object"
        ? sourceProgress.active_unit
        : {},
      updated_at: sourceProgress.updated_at ?? new Date().toISOString(),
    } : undefined;
    return this.post(`/v1/workers/${encodeURIComponent(this.workerId)}/heartbeat`, {
      protocol_version: "review-worker-protocol/v1",
      worker_id: this.workerId,
      status: wireStatus,
      active_run_id: state.activeRunId,
      concurrency: {
        max_active_jobs: 1,
        active_jobs: active ? 1 : 0,
        available_job_slots: active ? 0 : 1,
        maintains_local_queue: false,
        local_queue_depth: 0,
      },
      agent_session: {
        status: active ? "running" : "idle",
        transport: "embedded",
        active_session_id: state.activeSessionId,
      },
      provider: readyProviders[0] ?? "unconfigured",
      providerChain: readyProviders,
      readyProviders,
      version: this.workerVersion,
      hostname: this.hostname,
      doctor_status: state.status === "degraded" || !readyProviders.length ? "not_ready" : "ok",
      last_error: state.lastError ?? null,
      runtime_catalog: catalog,
      ...(profileState ? { profile_state: gatewayProfileState(profileState) } : {}),
      ...(progress ? { progress } : {}),
    }, signal);
  }

  async lease(signal?: AbortSignal): Promise<Record<string, unknown>> {
    const payload = await this.post(`/v1/workers/${encodeURIComponent(this.workerId)}/lease`, {
      protocol_version: "review-worker-protocol/v1",
      worker_id: this.workerId,
      capacity: {
        available_job_slots: 1,
        active_jobs: 0,
        maintains_local_queue: false,
        local_queue_depth: 0,
      },
      capabilities: {
        full_repo_scan: true,
        pi_agent_session: true,
        isolated_pi_profiles: true,
        progress_events: true,
        cancellation: true,
        intent_test_validation: true,
      },
    }, signal);
    return payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
  }

  uploadArtifact(runId: string, payload: object, signal?: AbortSignal): Promise<unknown> {
    const safeRunId = safeIdentifier(runId, "runId");
    return this.post(
      `/v1/review-runs/${encodeURIComponent(safeRunId)}/artifacts`,
      payload,
      signal,
    );
  }

  async submitResult(runId: string, payload: object, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const safeRunId = safeIdentifier(runId, "runId");
    const response = await this.post(
      `/v1/review-runs/${encodeURIComponent(safeRunId)}/result`,
      payload,
      signal,
    );
    return response && typeof response === "object" ? response as Record<string, unknown> : {};
  }
}
