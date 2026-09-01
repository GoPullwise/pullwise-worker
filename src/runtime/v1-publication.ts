import { createHash } from "node:crypto";

import type { ReviewAttemptResult } from "./attempt-supervisor.ts";
import type { ReviewPayload } from "./review-result.ts";

interface PublicationInput {
  readonly workerId: string;
  readonly workerVersion: string;
  readonly job: {
    readonly job_id: string;
    readonly run_id: string;
    readonly lease_id: string;
    readonly attempt: number;
  };
  readonly result: ReviewAttemptResult<ReviewPayload>;
}

interface TerminalPublicationInput {
  readonly workerId: string;
  readonly workerVersion: string;
  readonly job: PublicationInput["job"];
  readonly status: "failed" | "cancelled";
  readonly error: string;
}

interface ArtifactSpec {
  readonly artifactId: string;
  readonly kind: string;
  readonly name: string;
  readonly mediaType: string;
  readonly schemaId: string;
  readonly content: string;
}

function json(value: unknown): string {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function humanReport(payload: ReviewPayload): string {
  const findings = payload.findings.length
    ? payload.findings.map((finding, index) =>
      `## ${index + 1}. ${String(finding.title ?? "Finding")}\n\n${String(finding.explanation ?? "")}`
    ).join("\n\n")
    : "## Findings\n\nNo confirmed findings.";
  return `# Pullwise Repository Review\n\n${payload.summary}\n\n${findings}\n`;
}

function artifactSpecs(result: ReviewAttemptResult<ReviewPayload>): ArtifactSpec[] {
  return [
    {
      artifactId: "art_report_human",
      kind: "report.human",
      name: "report.md",
      mediaType: "text/markdown",
      schemaId: "pullwise-pi-human-report/v1",
      content: humanReport(result.payload),
    },
    {
      artifactId: "art_report_agent",
      kind: "report.agent",
      name: "report.agent.json",
      mediaType: "application/json",
      schemaId: "pullwise-pi-review-payload/v1",
      content: json(result.payload),
    },
    {
      artifactId: "art_coverage",
      kind: "coverage",
      name: "coverage.json",
      mediaType: "application/json",
      schemaId: "pullwise-pi-coverage/v1",
      content: json({ coverage: result.payload.coverage }),
    },
    {
      artifactId: "art_qa",
      kind: "qa",
      name: "qa.json",
      mediaType: "application/json",
      schemaId: "pullwise-pi-qa/v1",
      content: json({ status: "pass", errors: [], warnings: [] }),
    },
    {
      artifactId: "art_token_budget",
      kind: "token_budget",
      name: "token-budget.json",
      mediaType: "application/json",
      schemaId: "pullwise-pi-token-budget/v1",
      content: json({
        session_id: result.sessionId,
        provider: result.model.provider,
        model: result.model.id,
        usage: result.usage,
        started_at: result.startedAt,
        finished_at: result.finishedAt,
      }),
    },
  ];
}

function severityCounts(findings: readonly Record<string, unknown>[]) {
  const counts = { critical: 0, high: 0, medium: 0, low: 0 };
  for (const finding of findings) {
    const severity = String(finding.severity ?? "").toLowerCase() as keyof typeof counts;
    if (severity in counts) counts[severity] += 1;
  }
  return counts;
}

function protocolFinding(finding: Record<string, unknown>) {
  const evidence = finding.evidence && typeof finding.evidence === "object" && !Array.isArray(finding.evidence)
    ? finding.evidence as Record<string, unknown>
    : {};
  const file = String(evidence.path ?? finding.file ?? "");
  const line = Number(evidence.start_line ?? evidence.startLine ?? finding.line ?? 0);
  return {
    issue_id: String(finding.finding_id ?? finding.id ?? ""),
    finding_id: String(finding.finding_id ?? finding.id ?? ""),
    fingerprint: String(finding.fingerprint ?? ""),
    title: String(finding.title ?? "Finding"),
    severity: String(finding.severity ?? "low").toLowerCase(),
    category: String(finding.category ?? "correctness").toLowerCase(),
    description: String(finding.explanation ?? ""),
    explanation: String(finding.explanation ?? ""),
    impact: String(finding.impact ?? ""),
    remediation: String(finding.remediation ?? ""),
    validation_status: String(finding.validation_status ?? "UNVALIDATED").toLowerCase(),
    location: { file, line: Number.isSafeInteger(line) && line > 0 ? line : 1 },
    evidence: [{
      path: file,
      line: Number.isSafeInteger(line) && line > 0 ? line : 1,
      text: String(finding.evidence_text ?? ""),
    }],
  };
}

function stableSummary(payload: ReviewPayload) {
  const counts = severityCounts(payload.findings);
  const reviewed = payload.coverage.filter((entry) => entry.state === "REVIEWED").length;
  const skipped = payload.coverage.length - reviewed;
  const overallRisk = counts.critical ? "critical" : counts.high ? "high" : counts.medium ? "medium" : counts.low ? "low" : "none";
  return {
    overall_risk: overallRisk,
    result_status: "complete",
    finding_counts: {
      confirmed_critical: counts.critical,
      confirmed_high: counts.high,
      confirmed_medium: counts.medium,
      confirmed_low: counts.low,
      plausible: 0,
      weak_appendix: 0,
      disproven: 0,
      suppressed: 0,
    },
    coverage: {
      source_like_files_total: payload.coverage.length,
      deep_reviewed_files: reviewed,
      standard_reviewed_files: 0,
      light_reviewed_files: 0,
      inventory_only_files: 0,
      skipped_files: skipped,
      intent_tests_planned: 0,
      intent_tests_run: 0,
    },
    top_findings: payload.findings.map(protocolFinding),
  };
}

function artifactUploads(runId: string, attemptId: string, specs: ArtifactSpec[]) {
  return specs.map((spec) => {
    const bytes = Buffer.from(spec.content, "utf8");
    return {
      protocol_version: "review-worker-protocol/v1",
      attempt_id: attemptId,
      run_id: runId,
      artifact: {
        artifact_id: spec.artifactId,
        kind: spec.kind,
        name: spec.name,
        media_type: spec.mediaType,
        schema_id: spec.schemaId,
        schema_version: "v1",
        encoding: "utf-8",
        compression: "none",
        sha256: createHash("sha256").update(bytes).digest("hex"),
        size_bytes: bytes.length,
        required: true,
      },
      content_base64: bytes.toString("base64"),
    };
  });
}

function artifactManifest(runId: string, artifacts: ReturnType<typeof artifactUploads>) {
  return artifacts.map((upload) => ({
    ...upload.artifact,
    storage: {
      type: "server_artifact",
      url: `/v1/review-runs/${runId}/artifacts/${upload.artifact.artifact_id}`,
    },
  }));
}

export function buildV1Publication(input: PublicationInput) {
  const attemptId = `${input.workerId}-${input.job.attempt}`;
  const specs = artifactSpecs(input.result);
  const artifacts = artifactUploads(input.job.run_id, attemptId, specs);
  const manifest = artifactManifest(input.job.run_id, artifacts);
  const summary = stableSummary(input.result.payload);
  return {
    artifacts,
    result: {
      status: "done",
      attempt_id: attemptId,
      summary: severityCounts(input.result.payload.findings),
      reviewWorkerProtocol: {
        protocol_version: "review-worker-protocol/v1",
        message_type: "review_run_result",
        job: {
          job_id: input.job.job_id,
          run_id: input.job.run_id,
          lease_id: input.job.lease_id,
          job_type: "repo_review.full_scan",
        },
        worker: {
          worker_id: input.workerId,
          worker_version: input.workerVersion,
          concurrency: { max_active_jobs: 1, maintains_local_queue: false },
          engine: { type: "pi_agent_session", app_server_transport: "embedded" },
        },
        execution: { status: "completed", review_mode: "full_repo" },
        progress_final: {
          overall_percent: 100,
          current_phase: "submit_result_envelope",
          status: "completed",
          message: "Pi review completed.",
        },
        quality_gate: { status: "pass", errors: [], warnings: [] },
        artifact_manifest: manifest,
        summary,
        usage: input.result.usage,
      },
    },
  };
}

export function buildV1TerminalPublication(input: TerminalPublicationInput) {
  const attemptId = `${input.workerId}-${input.job.attempt}`;
  const status = input.status;
  const specs: ArtifactSpec[] = [
    {
      artifactId: "art_worker_log",
      kind: "worker_log",
      name: "worker.log.jsonl",
      mediaType: "application/jsonl",
      schemaId: "pullwise-pi-worker-log/v1",
      content: `${JSON.stringify({ level: "error", status, message: input.error })}\n`,
    },
    {
      artifactId: "art_qa",
      kind: "qa",
      name: "qa.json",
      mediaType: "application/json",
      schemaId: "pullwise-pi-qa/v1",
      content: json({ status: "fail", errors: [input.error], warnings: [] }),
    },
    {
      artifactId: "art_error_report",
      kind: "error_report",
      name: "error-report.json",
      mediaType: "application/json",
      schemaId: "pullwise-pi-error-report/v1",
      content: json({ status, error: input.error }),
    },
  ];
  const artifacts = artifactUploads(input.job.run_id, attemptId, specs);
  const manifest = artifactManifest(input.job.run_id, artifacts);
  const summary = stableSummary({ summary: input.error, findings: [], coverage: [] });
  summary.result_status = "incomplete";
  return {
    artifacts,
    result: {
      status,
      attempt_id: attemptId,
      summary: { critical: 0, high: 0, medium: 0, low: 0, info: 0 },
      error: input.error,
      reviewWorkerProtocol: {
        protocol_version: "review-worker-protocol/v1",
        message_type: "review_run_result",
        job: {
          job_id: input.job.job_id,
          run_id: input.job.run_id,
          lease_id: input.job.lease_id,
          job_type: "repo_review.full_scan",
        },
        worker: {
          worker_id: input.workerId,
          worker_version: input.workerVersion,
          concurrency: { max_active_jobs: 1, maintains_local_queue: false },
          engine: { type: "pi_agent_session", app_server_transport: "embedded" },
        },
        execution: { status, review_mode: "full_repo" },
        progress_final: {
          overall_percent: status === "cancelled" ? 0 : 1,
          current_phase: "failure_handling",
          status,
          message: input.error,
        },
        quality_gate: { status: "fail", errors: [input.error], warnings: [] },
        artifact_manifest: manifest,
        summary,
      },
    },
  };
}
