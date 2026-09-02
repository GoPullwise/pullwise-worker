import { lstat, realpath } from "node:fs/promises";
import path from "node:path";

export interface ReviewUsage {
  readonly input: number;
  readonly output: number;
  readonly cacheRead: number;
  readonly cacheWrite: number;
  readonly total: number;
  readonly cost: number;
}

export interface ReviewBudget {
  readonly wallTimeMs: number;
  readonly inputTokens: number;
  readonly outputTokens: number;
  readonly cacheReadTokens: number;
  readonly cacheWriteTokens: number;
}

export interface ReviewAttempt {
  readonly attemptId: string;
  readonly workspace: string;
  readonly provider: string;
  readonly model: string;
  readonly thinkingLevel: "low" | "medium" | "high";
  readonly context: Readonly<Record<string, unknown>>;
  readonly budget: ReviewBudget;
}

export type ReviewSessionEvent = {
  readonly type: "usage";
  /** Cumulative immutable usage for this session. */
  readonly usage: ReviewUsage;
};

export interface ReviewSession {
  readonly sessionId: string;
  readonly model: {
    readonly provider: string;
    readonly id: string;
  };
  subscribe(listener: (event: ReviewSessionEvent) => void): () => void;
  prompt(text: string): Promise<void>;
  abort(): Promise<void>;
  getLastAssistantText(): string | undefined;
  getUsage(): ReviewUsage;
  dispose(): void;
}

export interface ReviewAttemptResult<T> {
  readonly attemptId: string;
  readonly sessionId: string;
  readonly model: ReviewSession["model"];
  readonly usage: ReviewUsage;
  readonly startedAt: number;
  readonly finishedAt: number;
  readonly payload: T;
}

export interface ReviewSupervisorDependencies<T> {
  createSession(attempt: ReviewAttempt): Promise<ReviewSession>;
  renderPrompt(attempt: ReviewAttempt): Promise<string>;
  validateResult(text: string): T;
  validateFence(attempt: ReviewAttempt): Promise<boolean>;
  now?: () => number;
}

export class AttemptCancelledError extends Error {
  override readonly name = "AttemptCancelledError";
}

export class AttemptDeadlineExceededError extends Error {
  override readonly name = "AttemptDeadlineExceededError";
}

export class AttemptBudgetExceededError extends Error {
  override readonly name = "AttemptBudgetExceededError";
}

export class AttemptSupersededError extends Error {
  override readonly name = "AttemptSupersededError";
}

export class UnsafeWorkspaceError extends Error {
  override readonly name = "UnsafeWorkspaceError";
}

export class InvalidReviewResultError extends Error {
  override readonly name = "InvalidReviewResultError";
}

const REPARSE_POINT = 0x400;

function isPositiveInteger(value: number): boolean {
  return Number.isSafeInteger(value) && value > 0;
}

function assertAttempt(attempt: ReviewAttempt): void {
  for (const [label, value] of [
    ["attemptId", attempt.attemptId],
    ["provider", attempt.provider],
    ["model", attempt.model],
  ] as const) {
    if (!value.trim()) throw new TypeError(`${label} must be a non-empty string`);
  }
  if (!["low", "medium", "high"].includes(attempt.thinkingLevel)) {
    throw new TypeError("thinkingLevel must be low, medium, or high");
  }
  for (const [label, value] of Object.entries(attempt.budget)) {
    if (!isPositiveInteger(value)) {
      throw new TypeError(`budget.${label} must be a positive safe integer`);
    }
  }
  if (!attempt.context || Array.isArray(attempt.context)) {
    throw new TypeError("context must be an object");
  }
}

async function assertSafeWorkspace(workspace: string): Promise<string> {
  const lexical = path.resolve(workspace);
  try {
    const metadata = await lstat(lexical);
    const attributes = (metadata as typeof metadata & { fileAttributes?: number }).fileAttributes ?? 0;
    const resolved = await realpath(lexical);
    if (
      !metadata.isDirectory() ||
      metadata.isSymbolicLink() ||
      Boolean(attributes & REPARSE_POINT) ||
      path.relative(lexical, resolved) !== "" ||
      path.relative(resolved, lexical) !== ""
    ) {
      throw new UnsafeWorkspaceError("workspace must be a real, non-linked directory");
    }
    return resolved;
  } catch (error) {
    if (error instanceof UnsafeWorkspaceError) throw error;
    const detail = error instanceof Error ? error.message : String(error);
    throw new UnsafeWorkspaceError(`workspace is missing or unreadable: ${detail}`);
  }
}

function budgetError(usage: ReviewUsage, budget: ReviewBudget): AttemptBudgetExceededError | undefined {
  const limits: ReadonlyArray<readonly [string, number, number]> = [
    ["input", usage.input, budget.inputTokens],
    ["output", usage.output, budget.outputTokens],
    ["cacheRead", usage.cacheRead, budget.cacheReadTokens],
    ["cacheWrite", usage.cacheWrite, budget.cacheWriteTokens],
  ];
  const exceeded = limits.find(([, actual, limit]) => actual > limit);
  if (!exceeded) return undefined;
  const [kind, actual, limit] = exceeded;
  return new AttemptBudgetExceededError(`${kind} token budget exceeded: ${actual} > ${limit}`);
}

export async function runReviewAttempt<T>(
  input: ReviewAttempt,
  dependencies: ReviewSupervisorDependencies<T>,
  options: {
    signal?: AbortSignal;
    onSessionStarted?: (session: Pick<ReviewSession, "sessionId" | "model">) => void | Promise<void>;
  } = {},
): Promise<ReviewAttemptResult<T>> {
  assertAttempt(input);
  if (options.signal?.aborted) {
    throw new AttemptCancelledError("attempt was cancelled before session creation");
  }
  const workspace = await assertSafeWorkspace(input.workspace);
  const attempt = { ...input, workspace };
  const now = dependencies.now ?? Date.now;
  const startedAt = now();
  const prompt = await dependencies.renderPrompt(attempt);
  if (!prompt.trim()) throw new TypeError("review prompt must not be empty");
  if (options.signal?.aborted) {
    throw new AttemptCancelledError("attempt was cancelled before session creation");
  }
  if (!(await dependencies.validateFence(attempt))) {
    throw new AttemptSupersededError("attempt authority was stale before session creation");
  }
  if (options.signal?.aborted) {
    throw new AttemptCancelledError("attempt was cancelled before session creation");
  }

  const session = await dependencies.createSession(attempt);
  const remainingWallTime = attempt.budget.wallTimeMs - (now() - startedAt);
  if (options.signal?.aborted || remainingWallTime <= 0) {
    try {
      await session.abort();
    } finally {
      session.dispose();
    }
    if (options.signal?.aborted) {
      throw new AttemptCancelledError("attempt was cancelled during session creation");
    }
    throw new AttemptDeadlineExceededError("attempt wall-time budget expired during session creation");
  }
  await options.onSessionStarted?.({ sessionId: session.sessionId, model: session.model });
  let terminalError: Error | undefined;
  let abortPromise: Promise<void> | undefined;
  const requestAbort = (error: Error): void => {
    if (terminalError) return;
    terminalError = error;
    abortPromise = Promise.resolve().then(() => session.abort()).catch(() => undefined);
  };
  const unsubscribe = session.subscribe((event) => {
    const error = budgetError(event.usage, attempt.budget);
    if (error) requestAbort(error);
  });
  const onCancel = () => requestAbort(new AttemptCancelledError("attempt was cancelled"));
  options.signal?.addEventListener("abort", onCancel, { once: true });
  const deadline = setTimeout(
    () => requestAbort(new AttemptDeadlineExceededError("attempt wall-time budget exceeded")),
    remainingWallTime,
  );

  try {
    let promptFailure: unknown;
    try {
      await session.prompt(prompt);
    } catch (error) {
      promptFailure = error;
    }
    if (abortPromise) await abortPromise;
    if (terminalError) throw terminalError;
    if (promptFailure) throw promptFailure;

    const usage = session.getUsage();
    const finalBudgetError = budgetError(usage, attempt.budget);
    if (finalBudgetError) throw finalBudgetError;
    const text = session.getLastAssistantText();
    if (!text?.trim()) throw new InvalidReviewResultError("Pi returned no assistant result");
    const payload = dependencies.validateResult(text);
    if (terminalError) throw terminalError;
    if (!(await dependencies.validateFence(attempt))) {
      throw new AttemptSupersededError("attempt authority was superseded before publication");
    }
    if (terminalError) throw terminalError;
    return {
      attemptId: attempt.attemptId,
      sessionId: session.sessionId,
      model: session.model,
      usage,
      startedAt,
      finishedAt: now(),
      payload,
    };
  } finally {
    clearTimeout(deadline);
    options.signal?.removeEventListener("abort", onCancel);
    unsubscribe();
    session.dispose();
  }
}
