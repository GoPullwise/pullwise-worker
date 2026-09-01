import {
  runReviewAttempt,
  type ReviewAttempt,
  type ReviewAttemptResult,
  type ReviewSession,
} from "./attempt-supervisor.ts";
import {
  loadReviewCapability,
  renderReviewPrompt,
} from "./review-capability.ts";
import { createPiReviewSessionFactory } from "./pi-session.ts";
import { parseReviewPayload, type ReviewPayload } from "./review-result.ts";

export interface ReviewRunnerOptions {
  readonly agentDir: string;
  readonly capabilityRoot?: string;
  readonly createSession?: (attempt: ReviewAttempt) => Promise<ReviewSession>;
  readonly validateFence: (attempt: ReviewAttempt) => Promise<boolean>;
}

export type ReviewRunner = (
  attempt: ReviewAttempt,
  options?: {
    signal?: AbortSignal;
    onSessionStarted?: (session: Pick<ReviewSession, "sessionId" | "model">) => void | Promise<void>;
  },
) => Promise<ReviewAttemptResult<ReviewPayload>>;

export async function createReviewRunner(options: ReviewRunnerOptions): Promise<ReviewRunner> {
  const capability = await loadReviewCapability(options.capabilityRoot);
  const createSession = options.createSession ?? createPiReviewSessionFactory({
    agentDir: options.agentDir,
    capability,
  });
  return (attempt, runOptions = {}) => runReviewAttempt(attempt, {
    createSession,
    renderPrompt: async (current) => renderReviewPrompt(current, capability),
    validateResult: parseReviewPayload,
    validateFence: options.validateFence,
  }, runOptions);
}
