import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  AttemptBudgetExceededError,
  AttemptCancelledError,
  AttemptDeadlineExceededError,
  AttemptSupersededError,
  UnsafeWorkspaceError,
  runReviewAttempt,
  type ReviewAttempt,
  type ReviewSession,
  type ReviewSessionEvent,
  type ReviewUsage,
} from "../../src/runtime/attempt-supervisor.ts";

const ZERO_USAGE: ReviewUsage = Object.freeze({
  input: 0,
  output: 0,
  cacheRead: 0,
  cacheWrite: 0,
  total: 0,
  cost: 0,
});

class FakeSession implements ReviewSession {
  readonly sessionId = "session-1";
  readonly model = Object.freeze({ provider: "test-provider", id: "test-model" });
  promptCalls: string[] = [];
  abortCalls = 0;
  disposed = false;
  finalText = '{"summary":"ok","findings":[],"coverage":[]}';
  usage = ZERO_USAGE;
  onPrompt: () => Promise<void> = async () => {};
  onAbort: () => void = () => {};
  private readonly listeners = new Set<(event: ReviewSessionEvent) => void>();

  subscribe(listener: (event: ReviewSessionEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  emit(event: ReviewSessionEvent): void {
    for (const listener of this.listeners) listener(event);
  }

  async prompt(text: string): Promise<void> {
    this.promptCalls.push(text);
    await this.onPrompt();
  }

  async abort(): Promise<void> {
    this.abortCalls += 1;
    this.onAbort();
  }

  getLastAssistantText(): string | undefined {
    return this.finalText;
  }

  getUsage(): ReviewUsage {
    return this.usage;
  }

  dispose(): void {
    this.disposed = true;
  }
}

async function withWorkspace(run: (workspace: string) => Promise<void>): Promise<void> {
  const workspace = await mkdtemp(join(tmpdir(), "pullwise-pi-supervisor-"));
  try {
    await run(workspace);
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
}

function attempt(workspace: string, overrides: Partial<ReviewAttempt> = {}): ReviewAttempt {
  return {
    attemptId: "4f17b7fc-80d6-4a33-9d34-3ea3b8468141",
    workspace,
    provider: "test-provider",
    model: "test-model",
    thinkingLevel: "medium",
    context: { repository: "example/repo", revision: "abc123" },
    budget: {
      wallTimeMs: 1_000,
      inputTokens: 100,
      outputTokens: 100,
      cacheReadTokens: 100,
      cacheWriteTokens: 100,
    },
    ...overrides,
  };
}

function dependencies(session: FakeSession, overrides: Record<string, unknown> = {}) {
  let creates = 0;
  const deps = {
    createSession: async () => {
      creates += 1;
      return session;
    },
    renderPrompt: async () => "rendered review prompt",
    validateResult: (text: string) => JSON.parse(text) as unknown,
    validateFence: async () => true,
    ...overrides,
  };
  return { deps, creates: () => creates };
}

test("one attempt owns one Pi session and one prompt", async () => {
  await withWorkspace(async (workspace) => {
    const session = new FakeSession();
    session.usage = { ...ZERO_USAGE, input: 12, output: 7, total: 19, cost: 0.02 };
    const { deps, creates } = dependencies(session);
    const startedSessions: string[] = [];

    const result = await runReviewAttempt(attempt(workspace), deps, {
      onSessionStarted: async (started) => {
        startedSessions.push(started.sessionId);
      },
    });

    assert.equal(creates(), 1);
    assert.deepEqual(session.promptCalls, ["rendered review prompt"]);
    assert.equal(result.sessionId, "session-1");
    assert.deepEqual(result.model, session.model);
    assert.deepEqual(result.usage, session.usage);
    assert.deepEqual(result.payload, { summary: "ok", findings: [], coverage: [] });
    assert.equal(session.abortCalls, 0);
    assert.equal(session.disposed, true);
    assert.deepEqual(startedSessions, ["session-1"]);
  });
});

test("token budget observation aborts the Pi session without another turn", async () => {
  await withWorkspace(async (workspace) => {
    const session = new FakeSession();
    let releasePrompt!: () => void;
    session.onPrompt = () => new Promise<void>((resolve) => {
      releasePrompt = resolve;
      session.emit({
        type: "usage",
        usage: { ...ZERO_USAGE, input: 11, total: 11 },
      });
    });
    session.onAbort = () => releasePrompt();
    const { deps } = dependencies(session);
    const limited = attempt(workspace, {
      budget: { ...attempt(workspace).budget, inputTokens: 10 },
    });

    await assert.rejects(runReviewAttempt(limited, deps), AttemptBudgetExceededError);
    assert.equal(session.abortCalls, 1);
    assert.equal(session.promptCalls.length, 1);
    assert.equal(session.disposed, true);
  });
});

test("deadline aborts a running session", async () => {
  await withWorkspace(async (workspace) => {
    const session = new FakeSession();
    let releasePrompt!: () => void;
    session.onPrompt = () => new Promise<void>((resolve) => {
      releasePrompt = resolve;
    });
    session.onAbort = () => releasePrompt();
    const { deps } = dependencies(session);

    await assert.rejects(
      runReviewAttempt(attempt(workspace, {
        budget: { ...attempt(workspace).budget, wallTimeMs: 5 },
      }), deps),
      AttemptDeadlineExceededError,
    );
    assert.equal(session.abortCalls, 1);
    assert.equal(session.disposed, true);
  });
});

test("an already-cancelled attempt never creates a session", async () => {
  await withWorkspace(async (workspace) => {
    const controller = new AbortController();
    controller.abort();
    const session = new FakeSession();
    const { deps, creates } = dependencies(session);

    await assert.rejects(
      runReviewAttempt(attempt(workspace), deps, { signal: controller.signal }),
      AttemptCancelledError,
    );
    assert.equal(creates(), 0);
  });
});

test("cancellation racing with session creation aborts and disposes that session", async () => {
  await withWorkspace(async (workspace) => {
    const controller = new AbortController();
    const session = new FakeSession();
    const { deps } = dependencies(session, {
      createSession: async () => {
        controller.abort();
        return session;
      },
    });

    await assert.rejects(
      runReviewAttempt(attempt(workspace), deps, { signal: controller.signal }),
      AttemptCancelledError,
    );
    assert.equal(session.abortCalls, 1);
    assert.equal(session.disposed, true);
  });
});

test("late fence rejection prevents a completed result from escaping", async () => {
  await withWorkspace(async (workspace) => {
    const session = new FakeSession();
    let fenceChecks = 0;
    const { deps } = dependencies(session, {
      validateFence: async () => {
        fenceChecks += 1;
        return fenceChecks === 1;
      },
    });

    await assert.rejects(runReviewAttempt(attempt(workspace), deps), AttemptSupersededError);
    assert.equal(fenceChecks, 2);
    assert.equal(session.disposed, true);
  });
});

test("stale initial fence prevents session creation", async () => {
  await withWorkspace(async (workspace) => {
    const session = new FakeSession();
    const { deps, creates } = dependencies(session, { validateFence: async () => false });
    await assert.rejects(runReviewAttempt(attempt(workspace), deps), AttemptSupersededError);
    assert.equal(creates(), 0);
  });
});

test("unsafe workspace rejection happens before session creation", async () => {
  const session = new FakeSession();
  const { deps, creates } = dependencies(session);
  await assert.rejects(
    runReviewAttempt(attempt(join(tmpdir(), "missing-pullwise-workspace")), deps),
    UnsafeWorkspaceError,
  );
  assert.equal(creates(), 0);
});
