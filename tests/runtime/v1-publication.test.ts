import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { buildV1Publication, buildV1TerminalPublication } from "../../src/runtime/v1-publication.ts";

test("Pi payload maps mechanically to the existing v1 artifacts and result envelope", () => {
  const publication = buildV1Publication({
    workerId: "wk_pi",
    workerVersion: "0.10.24",
    job: {
      job_id: "job_1",
      run_id: "run_1",
      lease_id: "lease_1",
      attempt: 1,
    },
    result: {
      attemptId: "attempt_1",
      sessionId: "pi_session_1",
      model: { provider: "openai", id: "gpt-5.1" },
      usage: { input: 10, output: 5, cacheRead: 2, cacheWrite: 1, total: 18, cost: 0.03 },
      startedAt: 100,
      finishedAt: 200,
      payload: { summary: "No findings.", findings: [], coverage: [] },
    },
  });

  assert.equal(publication.artifacts.length, 5);
  assert.deepEqual(publication.artifacts.map((item) => item.artifact.kind), [
    "report.human",
    "report.agent",
    "coverage",
    "qa",
    "token_budget",
  ]);
  for (const upload of publication.artifacts) {
    const content = Buffer.from(upload.content_base64, "base64");
    assert.equal(createHash("sha256").update(content).digest("hex"), upload.artifact.sha256);
    assert.equal(content.length, upload.artifact.size_bytes);
  }
  assert.equal(publication.result.status, "done");
  assert.equal(publication.result.reviewWorkerProtocol.worker.engine.type, "pi_agent_session");
  assert.equal(publication.result.reviewWorkerProtocol.summary.top_findings.length, 0);
  assert.equal(publication.result.reviewWorkerProtocol.artifact_manifest.length, 5);
  assert.doesNotMatch(JSON.stringify(publication), /codex/iu);
});

test("cancelled attempt maps to the three required terminal diagnostic artifacts", () => {
  const publication = buildV1TerminalPublication({
    workerId: "wk_pi",
    workerVersion: "0.10.24",
    job: { job_id: "job_1", run_id: "run_1", lease_id: "lease_1", attempt: 1 },
    status: "cancelled",
    error: "user_request",
  });
  assert.deepEqual(publication.artifacts.map((item) => item.artifact.kind), [
    "worker_log",
    "qa",
    "error_report",
  ]);
  assert.equal(publication.result.status, "cancelled");
  assert.equal(publication.result.reviewWorkerProtocol.execution.status, "cancelled");
  assert.equal(publication.result.reviewWorkerProtocol.worker.engine.type, "pi_agent_session");
});

test("finding evidence maps to the Server v1 top-finding shape", () => {
  const publication = buildV1Publication({
    workerId: "wk_pi",
    workerVersion: "0.10.24",
    job: { job_id: "job_1", run_id: "run_1", lease_id: "lease_1", attempt: 1 },
    result: {
      attemptId: "attempt_1",
      sessionId: "session_1",
      model: { provider: "anthropic", id: "model" },
      usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, total: 2, cost: 0 },
      startedAt: 1,
      finishedAt: 2,
      payload: {
        summary: "One finding.",
        coverage: [],
        findings: [{
          finding_id: "finding_1",
          title: "Broken boundary",
          severity: "HIGH",
          category: "CORRECTNESS",
          explanation: "The boundary fails.",
          impact: "Requests fail.",
          remediation: "Validate first.",
          validation_status: "VALIDATED",
          evidence: { path: "src/a.ts", start_line: 10 },
        }],
      },
    },
  });
  const finding = publication.result.reviewWorkerProtocol.summary.top_findings[0];
  assert.ok(finding);
  assert.deepEqual(finding.location, {
    file: "src/a.ts",
    line: 10,
  });
  assert.equal(finding.severity, "high");
});
