import { lstat, mkdir, readFile, realpath, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { TextDecoder } from "node:util";

import { parseStrictJson } from "./strict-json.ts";

const SCHEMA_ID = "pullwise-worker-state/v1";
const STATUSES = new Set(["idle", "busy", "degraded", "cancelling", "finishing"]);
const REPARSE_POINT = 0x400;

export interface WorkerStateInput {
  readonly status: string;
  readonly activeRunId: string | null;
  readonly activeSessionId: string | null;
  readonly lastError: string | null;
  readonly progress: Readonly<Record<string, unknown>> | null;
}

export interface WorkerState extends WorkerStateInput {
  readonly updatedAt: string;
}

async function stateRoot(root: string): Promise<string> {
  const lexical = path.resolve(root);
  await mkdir(lexical, { recursive: true, mode: 0o700 });
  const metadata = await lstat(lexical);
  const resolved = await realpath(lexical);
  const attributes = (metadata as typeof metadata & { fileAttributes?: number }).fileAttributes ?? 0;
  if (
    !metadata.isDirectory() ||
    metadata.isSymbolicLink() ||
    Boolean(attributes & REPARSE_POINT) ||
    path.relative(lexical, resolved) !== "" ||
    path.relative(resolved, lexical) !== ""
  ) {
    throw new Error("Worker state root must be a real, non-linked directory");
  }
  return resolved;
}

function nullableText(value: unknown, label: string): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || !value.trim() || value.length > 512) {
    throw new TypeError(`${label} is invalid`);
  }
  return value.trim();
}

function validateState(value: unknown): WorkerState {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("Worker state must be an object");
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  const expected = [
    "active_run_id",
    "active_session_id",
    "last_error",
    "progress",
    "schema_id",
    "status",
    "updated_at",
  ];
  if (JSON.stringify(keys) !== JSON.stringify(expected)) {
    throw new TypeError("Worker state must be a closed object");
  }
  const status = String(record.status ?? "");
  if (record.schema_id !== SCHEMA_ID || !STATUSES.has(status)) {
    throw new TypeError("Worker state schema or status is invalid");
  }
  const activeRunId = nullableText(record.active_run_id, "active_run_id");
  const activeSessionId = nullableText(record.active_session_id, "active_session_id");
  const lastError = nullableText(record.last_error, "last_error");
  const progress = record.progress;
  if (progress !== null && (!progress || typeof progress !== "object" || Array.isArray(progress))) {
    throw new TypeError("Worker state progress must be an object or null");
  }
  const active = ["busy", "cancelling", "finishing"].includes(status);
  if (active !== Boolean(activeRunId)) {
    throw new TypeError("Worker state active bindings do not match status");
  }
  if (active && (progress as Record<string, unknown> | null)?.run_id !== activeRunId) {
    throw new TypeError("Worker state progress run_id must match active_run_id");
  }
  if (!active && progress !== null) {
    throw new TypeError("inactive Worker state progress must be null");
  }
  const updatedAt = String(record.updated_at ?? "");
  if (!updatedAt || Number.isNaN(Date.parse(updatedAt))) {
    throw new TypeError("Worker state updated_at is invalid");
  }
  return Object.freeze({
    status,
    activeRunId,
    activeSessionId,
    lastError,
    progress: progress as Readonly<Record<string, unknown>> | null,
    updatedAt,
  });
}

export async function writeWorkerState(root: string, input: WorkerStateInput): Promise<void> {
  const resolvedRoot = await stateRoot(root);
  const document = {
    schema_id: SCHEMA_ID,
    status: input.status,
    active_run_id: input.activeRunId,
    active_session_id: input.activeSessionId,
    last_error: input.lastError,
    progress: input.progress,
    updated_at: new Date().toISOString(),
  };
  validateState(document);
  const temporary = path.join(resolvedRoot, `.worker-state-${process.pid}-${Date.now()}.tmp`);
  await writeFile(temporary, `${JSON.stringify(document)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  await rename(temporary, path.join(resolvedRoot, "worker-state.json"));
}

export async function readWorkerState(root: string): Promise<WorkerState> {
  const resolvedRoot = await stateRoot(root);
  const statePath = path.join(resolvedRoot, "worker-state.json");
  const metadata = await lstat(statePath);
  const resolved = await realpath(statePath);
  if (!metadata.isFile() || metadata.isSymbolicLink() || path.dirname(resolved) !== resolvedRoot) {
    throw new Error("Worker state file is unsafe");
  }
  const text = new TextDecoder("utf-8", { fatal: true }).decode(await readFile(resolved));
  return validateState(parseStrictJson(text));
}
