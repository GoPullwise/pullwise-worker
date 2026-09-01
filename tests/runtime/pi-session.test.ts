import assert from "node:assert/strict";
import test from "node:test";

import {
  PiReviewSession,
  READ_ONLY_TOOLS,
  type PiSessionPort,
} from "../../src/runtime/pi-session.ts";

class FakePiSession implements PiSessionPort {
  readonly sessionId = "pi-session";
  readonly model = { provider: "provider", id: "model" };
  readonly messages: unknown[] = [];
  promptCalls: string[] = [];
  abortCalls = 0;
  disposeCalls = 0;
  listener: ((event: { type: string; message?: { role?: string } }) => void) | undefined;

  subscribe(listener: (event: { type: string; message?: { role?: string } }) => void): () => void {
    this.listener = listener;
    return () => {
      this.listener = undefined;
    };
  }

  async prompt(text: string): Promise<void> {
    this.promptCalls.push(text);
  }

  async abort(): Promise<void> {
    this.abortCalls += 1;
  }

  getLastAssistantText(): string | undefined {
    return "result";
  }

  getSessionStats() {
    return {
      tokens: { input: 2, output: 3, cacheRead: 4, cacheWrite: 5, total: 14 },
      cost: 0.25,
    };
  }

  dispose(): void {
    this.disposeCalls += 1;
  }
}

test("Pi adapter exposes only the fixed read-only tool set", () => {
  assert.deepEqual(READ_ONLY_TOOLS, ["repo_read", "repo_grep", "repo_ls"]);
});

test("Pi adapter maps cumulative SDK usage and delegates lifecycle once", async () => {
  const pi = new FakePiSession();
  const session = new PiReviewSession(pi);
  const observed: unknown[] = [];
  session.subscribe((event) => observed.push(event));

  pi.listener?.({ type: "message_end", message: { role: "assistant" } });
  await session.prompt("review");
  await session.abort();
  session.dispose();

  assert.deepEqual(observed, [{
    type: "usage",
    usage: { input: 2, output: 3, cacheRead: 4, cacheWrite: 5, total: 14, cost: 0.25 },
  }]);
  assert.deepEqual(pi.promptCalls, ["review"]);
  assert.equal(pi.abortCalls, 1);
  assert.equal(pi.disposeCalls, 1);
});
