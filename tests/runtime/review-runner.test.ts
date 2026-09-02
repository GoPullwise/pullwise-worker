import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import type {
  ReviewSession,
  ReviewSessionEvent,
  ReviewUsage,
} from "../../src/runtime/attempt-supervisor.ts";
import { createFileFenceValidator } from "../../src/runtime/file-fence.ts";
import { createReviewRunner } from "../../src/runtime/review-runner.ts";

class CompletedSession implements ReviewSession {
  readonly sessionId = "session";
  readonly model = { provider: "provider", id: "model" };
  prompts: string[] = [];
  subscribe(_listener: (event: ReviewSessionEvent) => void): () => void {
    return () => {};
  }
  async prompt(text: string): Promise<void> {
    this.prompts.push(text);
  }
  async abort(): Promise<void> {}
  getLastAssistantText(): string {
    return '{"summary":"No findings.","findings":[],"coverage":[]}';
  }
  getUsage(): ReviewUsage {
    return { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, total: 2, cost: 0.01 };
  }
  dispose(): void {}
}

test("review runner composes text capability, one session, validation, and fence", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "pullwise-review-runner-"));
  try {
    const session = new CompletedSession();
    const runner = await createReviewRunner({
      agentDir: workspace,
      createSession: async () => session,
      validateFence: async () => true,
    });
    const result = await runner({
      attemptId: "4f17b7fc-80d6-4a33-9d34-3ea3b8468141",
      workspace,
      provider: "provider",
      model: "model",
      thinkingLevel: "medium",
      context: { repository: "example/repo", revision: "abc123" },
      budget: {
        wallTimeMs: 1_000,
        inputTokens: 100,
        outputTokens: 100,
        cacheReadTokens: 100,
        cacheWriteTokens: 100,
      },
    });

    assert.match(session.prompts[0] ?? "", /^\/skill:pullwise-repository-review/u);
    assert.equal(result.payload.summary, "No findings.");
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});

test("file fence observes external supersession", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-file-fence-"));
  try {
    const fencePath = join(root, "attempt.fence");
    await writeFile(fencePath, "lease-1\n", "utf8");
    const validate = await createFileFenceValidator(root, {
      relativePath: "attempt.fence",
      expected: "lease-1",
    });
    assert.equal(await validate(), true);
    await writeFile(fencePath, "lease-2\n", "utf8");
    assert.equal(await validate(), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
