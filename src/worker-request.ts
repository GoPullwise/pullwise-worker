import type { ReviewAttempt } from "./runtime/attempt-supervisor.ts";
import type { FileFence } from "./runtime/file-fence.ts";
import { parseStrictJson } from "./runtime/strict-json.ts";

export interface WorkerRequest {
  readonly attempt: ReviewAttempt;
  readonly fence: FileFence;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[], label: string): void {
  const actual = Object.keys(value).sort();
  if (JSON.stringify(actual) !== JSON.stringify([...expected].sort())) {
    throw new TypeError(`${label} has an invalid field set`);
  }
}

function nonEmptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) throw new TypeError(`${label} must be non-empty`);
  return value;
}

export function parseWorkerRequest(text: string): WorkerRequest {
  const root = record(parseStrictJson(text), "request");
  exactKeys(root, ["attempt", "fence"], "request");
  const rawAttempt = record(root.attempt, "attempt");
  exactKeys(
    rawAttempt,
    ["attemptId", "workspace", "provider", "model", "context", "budget"],
    "attempt",
  );
  const context = record(rawAttempt.context, "attempt.context");
  const budget = record(rawAttempt.budget, "attempt.budget");
  exactKeys(
    budget,
    ["wallTimeMs", "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"],
    "attempt.budget",
  );
  const rawFence = record(root.fence, "fence");
  exactKeys(rawFence, ["relativePath", "expected"], "fence");
  return Object.freeze({
    attempt: Object.freeze({
      attemptId: nonEmptyString(rawAttempt.attemptId, "attempt.attemptId"),
      workspace: nonEmptyString(rawAttempt.workspace, "attempt.workspace"),
      provider: nonEmptyString(rawAttempt.provider, "attempt.provider"),
      model: nonEmptyString(rawAttempt.model, "attempt.model"),
      context,
      budget: {
        wallTimeMs: budget.wallTimeMs as number,
        inputTokens: budget.inputTokens as number,
        outputTokens: budget.outputTokens as number,
        cacheReadTokens: budget.cacheReadTokens as number,
        cacheWriteTokens: budget.cacheWriteTokens as number,
      },
    }),
    fence: Object.freeze({
      relativePath: nonEmptyString(rawFence.relativePath, "fence.relativePath"),
      expected: nonEmptyString(rawFence.expected, "fence.expected"),
    }),
  });
}
