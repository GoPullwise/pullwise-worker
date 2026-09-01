import assert from "node:assert/strict";
import test from "node:test";

import { InvalidReviewResultError } from "../../src/runtime/attempt-supervisor.ts";
import { parseReviewPayload } from "../../src/runtime/review-result.ts";

test("accepts a strict minimal review payload", () => {
  assert.deepEqual(
    parseReviewPayload('{"summary":"No confirmed findings.","findings":[],"coverage":[]}'),
    { summary: "No confirmed findings.", findings: [], coverage: [] },
  );
});

for (const [name, body] of [
  ["markdown fencing", '```json\n{"summary":"x","findings":[],"coverage":[]}\n```'],
  ["duplicate keys", '{"summary":"x","summary":"y","findings":[],"coverage":[]}'],
  ["unknown fields", '{"summary":"x","findings":[],"coverage":[],"extra":true}'],
  ["invalid finding contract", '{"summary":"x","findings":[{}],"coverage":[]}'],
] as const) {
  test(`rejects ${name}`, () => {
    assert.throws(() => parseReviewPayload(body), InvalidReviewResultError);
  });
}
