import { validateDefinition } from "pullwise-review-contract";

import { InvalidReviewResultError } from "./attempt-supervisor.ts";
import { parseStrictJson, StrictJsonError } from "./strict-json.ts";

export interface ReviewPayload {
  readonly summary: string;
  readonly findings: readonly Record<string, unknown>[];
  readonly coverage: readonly Record<string, unknown>[];
}

function invalid(message: string): never {
  throw new InvalidReviewResultError(message);
}

function validateItems(definition: "Finding" | "CoverageEntry", items: unknown[]): void {
  for (const [index, item] of items.entries()) {
    const errors = validateDefinition(definition, item);
    if (errors.length) invalid(`${definition}[${index}] is invalid: ${JSON.stringify(errors[0])}`);
  }
}

function assertInvariants(findings: Record<string, unknown>[], coverage: Record<string, unknown>[]): void {
  const ordinals = findings.map((finding) => finding.ordinal);
  if (ordinals.some((value, index) => value !== index)) {
    invalid("finding ordinals must be dense from zero");
  }
  for (const key of ["finding_id", "fingerprint"] as const) {
    const values = findings.map((finding) => finding[key]);
    if (new Set(values).size !== values.length) invalid(`finding ${key} values must be unique`);
  }
  const paths = coverage.map((entry) => entry.path);
  if (new Set(paths).size !== paths.length) invalid("coverage paths must be unique");
  for (const entry of coverage) {
    if ((entry.state === "REVIEWED") !== (entry.reason_code === null)) {
      invalid("coverage reason_code must be null exactly when state is REVIEWED");
    }
  }
}

export function parseReviewPayload(text: string): ReviewPayload {
  let parsed: unknown;
  try {
    parsed = parseStrictJson(text);
  } catch (error) {
    if (error instanceof StrictJsonError) invalid(error.message);
    throw error;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return invalid("review result must be a JSON object");
  }
  const record = parsed as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  if (JSON.stringify(keys) !== JSON.stringify(["coverage", "findings", "summary"])) {
    return invalid("review result must contain exactly summary, findings, and coverage");
  }
  if (typeof record.summary !== "string" || record.summary.length < 1 || record.summary.length > 8_000) {
    return invalid("summary must contain 1 to 8000 characters");
  }
  if (!Array.isArray(record.findings) || record.findings.length > 1_000) {
    return invalid("findings must be an array with at most 1000 entries");
  }
  if (!Array.isArray(record.coverage) || record.coverage.length > 100_000) {
    return invalid("coverage must be an array with at most 100000 entries");
  }
  validateItems("Finding", record.findings);
  validateItems("CoverageEntry", record.coverage);
  const findings = record.findings as Record<string, unknown>[];
  const coverage = record.coverage as Record<string, unknown>[];
  assertInvariants(findings, coverage);
  return Object.freeze({ summary: record.summary, findings, coverage });
}
