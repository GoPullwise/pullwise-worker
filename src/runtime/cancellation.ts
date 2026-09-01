import { lstat, mkdir, readFile, realpath, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";

import { parseStrictJson } from "./strict-json.ts";

const SCHEMA_ID = "pullwise-worker-cancellation/v1";

function safeRunId(runId: string): string {
  const value = String(runId ?? "").trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u.test(value)) throw new TypeError("run_id is invalid");
  return value;
}

async function cancellationRoot(root: string): Promise<string> {
  const base = path.resolve(root);
  await mkdir(base, { recursive: true, mode: 0o700 });
  const resolvedBase = await realpath(base);
  if (path.relative(base, resolvedBase) !== "") throw new Error("state root is linked");
  const directory = path.join(resolvedBase, "cancellations");
  await mkdir(directory, { recursive: true, mode: 0o700 });
  return realpath(directory);
}

export async function requestCancellation(root: string, runId: string, reason: string): Promise<void> {
  const directory = await cancellationRoot(root);
  const safeId = safeRunId(runId);
  const safeReason = String(reason || "server_cancelled").trim().slice(0, 200);
  const document = {
    schema_id: SCHEMA_ID,
    run_id: safeId,
    reason: safeReason || "server_cancelled",
    requested_at: new Date().toISOString(),
  };
  const temporary = path.join(directory, `.${safeId}-${process.pid}-${Date.now()}.tmp`);
  await writeFile(temporary, `${JSON.stringify(document)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  await rename(temporary, path.join(directory, `${safeId}.json`));
}

export async function cancellationRequested(root: string, runId: string): Promise<boolean> {
  const directory = await cancellationRoot(root);
  const safeId = safeRunId(runId);
  const marker = path.join(directory, `${safeId}.json`);
  try {
    const metadata = await lstat(marker);
    const resolved = await realpath(marker);
    if (!metadata.isFile() || metadata.isSymbolicLink() || path.dirname(resolved) !== directory) return false;
    const parsed = parseStrictJson(await readFile(resolved, "utf8"));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return false;
    const record = parsed as Record<string, unknown>;
    return (
      JSON.stringify(Object.keys(record).sort()) ===
        JSON.stringify(["reason", "requested_at", "run_id", "schema_id"])
      && record.schema_id === SCHEMA_ID
      && record.run_id === safeId
      && typeof record.reason === "string"
      && typeof record.requested_at === "string"
    );
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw error;
  }
}

export async function clearCancellation(root: string, runId: string): Promise<void> {
  const directory = await cancellationRoot(root);
  await rm(path.join(directory, `${safeRunId(runId)}.json`), { force: true });
}
