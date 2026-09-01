import type {
  ReviewAttempt,
  ReviewAttemptResult,
  ReviewSession,
} from "./attempt-supervisor.ts";
import type { Profile, Profiles } from "./profiles.ts";
import type { ReviewPayload } from "./review-result.ts";
import { buildV1Publication, buildV1TerminalPublication } from "./v1-publication.ts";
import type { WorkerStateInput } from "./worker-state.ts";

export interface LeaseJob {
  readonly job_id: string;
  readonly run_id: string;
  readonly lease_id: string;
  readonly attempt: number;
  readonly runtime_selection?: {
    readonly credential_id?: string;
    readonly provider?: string;
    readonly model?: string;
  };
  readonly review_request?: {
    readonly budget?: {
      readonly max_wall_time_seconds?: number;
      readonly max_estimated_input_tokens?: number;
    };
  };
  readonly [key: string]: unknown;
}

interface MaterializedCheckout {
  readonly workspace: string;
  cleanup(): Promise<void>;
}

interface LeaseClient {
  uploadArtifact(runId: string, payload: object, signal?: AbortSignal): Promise<unknown>;
  submitResult(runId: string, payload: object, signal?: AbortSignal): Promise<Record<string, unknown>>;
}

interface ReviewOptions {
  readonly signal?: AbortSignal;
  readonly onSessionStarted?: (
    session: Pick<ReviewSession, "sessionId" | "model">,
  ) => void | Promise<void>;
}

interface ExecuteLeaseOptions {
  readonly workerId: string;
  readonly workerVersion: string;
  readonly stateRoot: string;
  readonly profiles: Profiles;
  readonly job: LeaseJob;
  readonly client: LeaseClient;
  readonly materialize: (job: LeaseJob, signal?: AbortSignal) => Promise<MaterializedCheckout>;
  readonly review: (
    attempt: ReviewAttempt,
    profile: Profile,
    options: ReviewOptions,
  ) => Promise<ReviewAttemptResult<ReviewPayload>>;
  readonly writeState: (state: WorkerStateInput) => Promise<void>;
  readonly signal?: AbortSignal;
}

function requiredText(value: unknown, label: string): string {
  const text = String(value ?? "").trim();
  if (!text) throw new Error(`${label} is required`);
  return text;
}

function selectedProfile(job: LeaseJob, profiles: Profiles): Profile {
  const selection = job.runtime_selection ?? {};
  const credentialId = requiredText(selection.credential_id, "runtime_selection.credential_id");
  const provider = requiredText(selection.provider, "runtime_selection.provider");
  requiredText(selection.model, "runtime_selection.model");
  const profile = profiles.profiles.find(
    (item) => item.credentialId === credentialId && item.provider === provider,
  );
  if (!profile) throw new Error("lease runtime selection does not match a local profile");
  return profile;
}

function progress(runId: string, phase: string, percent: number, message: string) {
  return {
    run_id: runId,
    overall_percent: percent,
    current_phase: phase,
    current_phase_status: "running",
    current_phase_percent: percent,
    message,
    counters: {},
    active_unit: {},
    last_event_sequence: 0,
    updated_at: new Date().toISOString(),
  };
}

function reviewAttempt(job: LeaseJob, workspace: string): ReviewAttempt {
  const selection = job.runtime_selection ?? {};
  const budget = job.review_request?.budget ?? {};
  const inputTokens = Math.max(1, Number(budget.max_estimated_input_tokens ?? 800_000));
  const wallTimeMs = Math.max(1_000, Number(budget.max_wall_time_seconds ?? 900) * 1_000);
  const { clone_token: _secret, ...safeJob } = job;
  return {
    attemptId: `${requiredText(job.job_id, "job_id")}-${Math.max(1, Number(job.attempt || 1))}`,
    workspace,
    provider: requiredText(selection.provider, "runtime_selection.provider"),
    model: requiredText(selection.model, "runtime_selection.model"),
    context: safeJob,
    budget: {
      wallTimeMs,
      inputTokens,
      outputTokens: Math.max(1, Math.ceil(inputTokens / 4)),
      cacheReadTokens: inputTokens,
      cacheWriteTokens: inputTokens,
    },
  };
}

export async function executeLeaseJob(
  options: ExecuteLeaseOptions,
): Promise<Record<string, unknown>> {
  const profile = selectedProfile(options.job, options.profiles);
  const runId = requiredText(options.job.run_id, "run_id");
  let checkout: MaterializedCheckout | undefined;
  let activeSessionId: string | null = null;
  await options.writeState({
    status: "busy",
    activeRunId: runId,
    activeSessionId,
    lastError: null,
    progress: progress(runId, "preparing", 0, "Preparing repository checkout."),
  });
  try {
    checkout = await options.materialize(options.job, options.signal);
    const attempt = reviewAttempt(options.job, checkout.workspace);
    const reviewOptions: ReviewOptions = {
      onSessionStarted: async (session) => {
        activeSessionId = session.sessionId;
        await options.writeState({
          status: "busy",
          activeRunId: runId,
          activeSessionId,
          lastError: null,
          progress: progress(runId, "review", 25, "Pi review session is running."),
        });
      },
    };
    if (options.signal) {
      (reviewOptions as { signal?: AbortSignal }).signal = options.signal;
    }
    const result = await options.review(attempt, profile, reviewOptions);
    await options.writeState({
      status: "finishing",
      activeRunId: runId,
      activeSessionId: result.sessionId,
      lastError: null,
      progress: progress(runId, "publishing", 90, "Publishing review artifacts."),
    });
    const publication = buildV1Publication({
      workerId: options.workerId,
      workerVersion: options.workerVersion,
      job: options.job,
      result,
    });
    for (const artifact of publication.artifacts) {
      await options.client.uploadArtifact(runId, artifact, options.signal);
    }
    const response = await options.client.submitResult(runId, publication.result, options.signal);
    await options.writeState({
      status: "idle",
      activeRunId: null,
      activeSessionId: null,
      lastError: null,
      progress: null,
    });
    return response;
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    try {
      const terminal = buildV1TerminalPublication({
        workerId: options.workerId,
        workerVersion: options.workerVersion,
        job: options.job,
        status: options.signal?.aborted ? "cancelled" : "failed",
        error: detail,
      });
      for (const artifact of terminal.artifacts) {
        await options.client.uploadArtifact(runId, artifact);
      }
      const response = await options.client.submitResult(runId, terminal.result);
      await options.writeState({
        status: "idle",
        activeRunId: null,
        activeSessionId: null,
        lastError: null,
        progress: null,
      });
      return response;
    } catch (publicationError) {
      await options.writeState({
        status: "degraded",
        activeRunId: null,
        activeSessionId: null,
        lastError: detail,
        progress: null,
      });
      throw publicationError;
    }
  } finally {
    await checkout?.cleanup();
  }
}
