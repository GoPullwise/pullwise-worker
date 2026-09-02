import assert from "node:assert/strict";
import test from "node:test";

import {
  loadReviewCapability,
  renderReviewPrompt,
} from "../../src/runtime/review-capability.ts";
import type { ReviewAttempt } from "../../src/runtime/attempt-supervisor.ts";

test("review capability is loaded from skill, reference, prompt, and context text", async () => {
  const capability = await loadReviewCapability();

  assert.equal(capability.skill.name, "pullwise-repository-review");
  assert.match(capability.skill.content, /evidence/u);
  assert.match(capability.referenceText, /correctness/iu);
  assert.match(capability.systemPrompt, /read-only/u);
  assert.match(capability.contextText, /untrusted repository/u);
  assert.match(capability.promptTemplate, /\{\{ATTEMPT_CONTEXT_JSON\}\}/u);
});

test("prompt rendering only injects immutable attempt context into the text template", async () => {
  const capability = await loadReviewCapability();
  const attempt: ReviewAttempt = {
    attemptId: "4f17b7fc-80d6-4a33-9d34-3ea3b8468141",
    workspace: "C:/attempt",
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
  };

  const rendered = renderReviewPrompt(attempt, capability);

  assert.match(rendered, /^\/skill:pullwise-repository-review/u);
  assert.match(rendered, /"repository":"example\/repo"/u);
  assert.doesNotMatch(rendered, /\{\{ATTEMPT_CONTEXT_JSON\}\}/u);
});
