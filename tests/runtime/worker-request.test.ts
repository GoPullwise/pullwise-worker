import assert from "node:assert/strict";
import test from "node:test";

import { parseWorkerRequest } from "../../src/worker-request.ts";

const valid = JSON.stringify({
  attempt: {
    attemptId: "4f17b7fc-80d6-4a33-9d34-3ea3b8468141",
    workspace: "C:/attempt",
    provider: "provider",
    model: "model",
    context: { repository: "example/repo" },
    budget: {
      wallTimeMs: 1_000,
      inputTokens: 100,
      outputTokens: 100,
      cacheReadTokens: 100,
      cacheWriteTokens: 100,
    },
  },
  fence: { relativePath: "attempt.fence", expected: "lease-1" },
});

test("parses the closed one-attempt Worker request", () => {
  const request = parseWorkerRequest(valid);
  assert.equal(request.attempt.provider, "provider");
  assert.equal(request.fence.expected, "lease-1");
});

for (const [name, body] of [
  ["unknown root field", `${valid.slice(0, -1)},"extra":true}`],
  ["duplicate root field", valid.replace("{", '{"attempt":null,')],
  ["missing budget field", valid.replace(',"cacheWriteTokens":100', "")],
] as const) {
  test(`rejects ${name}`, () => {
    assert.throws(() => parseWorkerRequest(body));
  });
}
