import { mkdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";

import { materializeCheckout, type CheckoutJob } from "./checkout.ts";
import { cancellationRequested, clearCancellation } from "./cancellation.ts";
import type { ControlPlaneClient } from "./control-plane.ts";
import { createFileFenceValidator } from "./file-fence.ts";
import { executeLeaseJob, type LeaseJob } from "./lease-job.ts";
import { loadProfiles } from "./profiles.ts";
import { createReviewRunner } from "./review-runner.ts";
import { writeWorkerState } from "./worker-state.ts";

interface LeaseOnlyClient {
  lease(signal?: AbortSignal): Promise<Record<string, unknown>>;
}

export interface WorkerOnceOptions {
  readonly client: LeaseOnlyClient;
  readonly executeJob: (job: Record<string, unknown>, signal?: AbortSignal) => Promise<void>;
  readonly signal?: AbortSignal;
}

export async function runWorkerOnce(options: WorkerOnceOptions): Promise<boolean> {
  const response = await options.client.lease(options.signal);
  const job = response.job;
  if (!job || typeof job !== "object" || Array.isArray(job)) return false;
  await options.executeJob(job as Record<string, unknown>, options.signal);
  return true;
}

function wait(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(done, milliseconds);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done, { once: true });
  });
}

export interface WorkerServiceOptions extends WorkerOnceOptions {
  readonly idlePollMs?: number;
  readonly failurePollMs?: number;
  readonly onError?: (error: Error) => void;
  readonly signal: AbortSignal;
}

export async function runWorkerService(options: WorkerServiceOptions): Promise<void> {
  while (!options.signal.aborted) {
    try {
      const worked = await runWorkerOnce(options);
      if (!worked) await wait(Math.max(1_000, options.idlePollMs ?? 5_000), options.signal);
    } catch (error) {
      options.onError?.(error instanceof Error ? error : new Error(String(error)));
      await wait(Math.max(1_000, options.failurePollMs ?? 10_000), options.signal);
    }
  }
}

export interface LeaseExecutorOptions {
  readonly client: ControlPlaneClient;
  readonly workerId: string;
  readonly workerVersion: string;
  readonly profileRoot: string;
  readonly stateRoot: string;
  readonly checkoutRoot: string;
}

function safeRunId(job: Record<string, unknown>): string {
  const value = String(job.run_id ?? "").trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u.test(value)) throw new Error("lease run_id is invalid");
  return value;
}

export function createLeaseExecutor(options: LeaseExecutorOptions) {
  return async (rawJob: Record<string, unknown>, signal?: AbortSignal): Promise<void> => {
    const job = rawJob as LeaseJob;
    const runId = safeRunId(rawJob);
    const leaseId = String(rawJob.lease_id ?? "").trim();
    if (!leaseId || /[\r\n]/u.test(leaseId)) throw new Error("lease_id is invalid");
    const fenceRoot = path.join(options.stateRoot, "fences");
    await mkdir(fenceRoot, { recursive: true, mode: 0o700 });
    const fenceName = `${runId}.fence`;
    const fencePath = path.join(fenceRoot, fenceName);
    await writeFile(fencePath, `${leaseId}\n`, { encoding: "utf8", mode: 0o600 });
    const attemptController = new AbortController();
    const forwardAbort = () => attemptController.abort();
    signal?.addEventListener("abort", forwardAbort, { once: true });
    let cancellationTimer: ReturnType<typeof setInterval> | undefined;
    const pollCancellation = async () => {
      if (await cancellationRequested(options.stateRoot, runId)) attemptController.abort();
    };
    await pollCancellation();
    cancellationTimer = setInterval(() => { void pollCancellation(); }, 500);
    try {
      const profiles = await loadProfiles(options.profileRoot);
      await executeLeaseJob({
        workerId: options.workerId,
        workerVersion: options.workerVersion,
        stateRoot: options.stateRoot,
        profiles,
        job,
        client: options.client,
        signal: attemptController.signal,
        materialize: (currentJob, currentSignal) => materializeCheckout(
          currentJob as CheckoutJob,
          {
            checkoutRoot: options.checkoutRoot,
            ...(currentSignal ? { signal: currentSignal } : {}),
          },
        ),
        review: async (attempt, profile, reviewOptions) => {
          const validateFence = await createFileFenceValidator(fenceRoot, {
            relativePath: fenceName,
            expected: leaseId,
          });
          const runner = await createReviewRunner({
            agentDir: profile.agentDir,
            validateFence: async () => validateFence(),
          });
          return runner(attempt, reviewOptions);
        },
        writeState: (state) => writeWorkerState(options.stateRoot, state),
      });
    } finally {
      if (cancellationTimer) clearInterval(cancellationTimer);
      signal?.removeEventListener("abort", forwardAbort);
      await clearCancellation(options.stateRoot, runId);
      await rm(fencePath, { force: true });
    }
  };
}
