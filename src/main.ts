#!/usr/bin/env node

import path from "node:path";
import process from "node:process";
import { hostname } from "node:os";
import { pathToFileURL } from "node:url";

import { ControlPlaneClient, exchangeWorkerBootstrap } from "./runtime/control-plane.ts";
import { requestCancellation } from "./runtime/cancellation.ts";
import { createFileFenceValidator } from "./runtime/file-fence.ts";
import { createReviewRunner } from "./runtime/review-runner.ts";
import { runWatcher, syncWatcherOnce } from "./runtime/watcher.ts";
import {
  applyGatewayProfile,
  loadGatewayProfileState,
  parseGatewayManifestTrust,
} from "./runtime/gateway-profile.ts";
import { createGatewayProfileReconciler } from "./runtime/gateway-profile-reconciler.ts";
import { createLeaseExecutor, runWorkerService } from "./runtime/worker-service.ts";
import { writeWorkerState } from "./runtime/worker-state.ts";
import { parseWorkerRequest } from "./worker-request.ts";

const MAX_REQUEST_BYTES = 1024 * 1024;

async function readRequest(): Promise<string> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.length;
    if (size > MAX_REQUEST_BYTES) throw new Error("Worker request exceeds 1 MiB");
    chunks.push(bytes);
  }
  if (size === 0) throw new Error("Worker request is empty");
  return Buffer.concat(chunks).toString("utf8");
}

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function controlPlaneClient(): ControlPlaneClient {
  return new ControlPlaneClient({
    serverUrl: requiredEnvironment("PULLWISE_SERVER_URL"),
    workerId: requiredEnvironment("PULLWISE_WORKER_ID"),
    token: requiredEnvironment("PULLWISE_WORKER_TOKEN"),
    workerVersion: process.env.PULLWISE_WORKER_VERSION?.trim() || "0.10.24",
    hostname: hostname(),
  });
}

async function modelProfileReconciler(client: ControlPlaneClient, profileRoot: string) {
  return createGatewayProfileReconciler({
    client,
    loadState: () => loadGatewayProfileState(profileRoot),
    applyProfile: async (payload, signal) => {
      const trust = parseGatewayManifestTrust(await client.fetchModelProfileTrust(signal));
      return applyGatewayProfile({
        profileRoot,
        expectedWorkerId: requiredEnvironment("PULLWISE_WORKER_ID"),
        manifestPublicKeys: trust,
        payload,
      });
    },
  });
}

export async function main(): Promise<number> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  process.once("SIGINT", cancel);
  process.once("SIGTERM", cancel);
  try {
    const request = parseWorkerRequest(await readRequest());
    const validateFence = await createFileFenceValidator(
      requiredEnvironment("PULLWISE_FENCE_ROOT"),
      request.fence,
    );
    const runner = await createReviewRunner({
      agentDir: requiredEnvironment("PULLWISE_PI_AGENT_DIR"),
      validateFence: async () => validateFence(),
    });
    const result = await runner(request.attempt, { signal: controller.signal });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return 0;
  } catch (error) {
    const failure = error instanceof Error ? error : new Error(String(error));
    process.stderr.write(`${JSON.stringify({ error: { name: failure.name, message: failure.message } })}\n`);
    return 1;
  } finally {
    process.removeListener("SIGINT", cancel);
    process.removeListener("SIGTERM", cancel);
  }
}

export async function cli(args = process.argv.slice(2)): Promise<number> {
  if (args[0] === "bootstrap") {
    const token = await exchangeWorkerBootstrap({
      serverUrl: requiredEnvironment("PULLWISE_SERVER_URL"),
      workerId: requiredEnvironment("PULLWISE_WORKER_ID"),
      bootstrapToken: requiredEnvironment("PULLWISE_WORKER_BOOTSTRAP_TOKEN"),
    });
    process.stdout.write(`${token}\n`);
    return 0;
  }
  if (args[0] === "profile") {
    throw new Error("profile commands are retired; profiles are centrally managed through Pullwise Model Gateway");
  }
  if (args[0] === "sync") {
    const client = controlPlaneClient();
    const profileRoot = requiredEnvironment("PULLWISE_PI_PROFILE_ROOT");
    await syncWatcherOnce({
      profileRoot,
      stateRoot: requiredEnvironment("PULLWISE_WORKER_STATE_ROOT"),
      client,
      register: true,
      reconcileProfile: await modelProfileReconciler(client, profileRoot),
    });
    process.stdout.write(`${JSON.stringify({ ok: true })}\n`);
    return 0;
  }
  if (args[0] === "watch") {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    process.once("SIGINT", cancel);
    process.once("SIGTERM", cancel);
    try {
      const client = controlPlaneClient();
      const profileRoot = requiredEnvironment("PULLWISE_PI_PROFILE_ROOT");
      await runWatcher({
        profileRoot,
        stateRoot: requiredEnvironment("PULLWISE_WORKER_STATE_ROOT"),
        client,
        reconcileProfile: await modelProfileReconciler(client, profileRoot),
        signal: controller.signal,
        onError: (error) => process.stderr.write(`${JSON.stringify({
          error: { name: error.name, message: error.message },
        })}\n`),
        handleCommand: async (command) => {
          if (command.type !== "cancel_run") return;
          await requestCancellation(
            requiredEnvironment("PULLWISE_WORKER_STATE_ROOT"),
            String(command.run_id ?? ""),
            String(command.reason ?? "server_cancelled"),
          );
        },
      });
      return 0;
    } finally {
      process.removeListener("SIGINT", cancel);
      process.removeListener("SIGTERM", cancel);
    }
  }
  if (args[0] === "serve") {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    process.once("SIGINT", cancel);
    process.once("SIGTERM", cancel);
    const stateRoot = requiredEnvironment("PULLWISE_WORKER_STATE_ROOT");
    const client = controlPlaneClient();
    try {
      await writeWorkerState(stateRoot, {
        status: "idle",
        activeRunId: null,
        activeSessionId: null,
        lastError: null,
        progress: null,
      });
      await runWorkerService({
        client,
        executeJob: createLeaseExecutor({
          client,
          workerId: requiredEnvironment("PULLWISE_WORKER_ID"),
          workerVersion: process.env.PULLWISE_WORKER_VERSION?.trim() || "0.10.24",
          profileRoot: requiredEnvironment("PULLWISE_PI_PROFILE_ROOT"),
          stateRoot,
          checkoutRoot: requiredEnvironment("PULLWISE_CHECKOUT_ROOT"),
        }),
        signal: controller.signal,
        onError: (error) => {
          void writeWorkerState(stateRoot, {
            status: "degraded",
            activeRunId: null,
            activeSessionId: null,
            lastError: error.message,
            progress: null,
          });
        },
      });
      return 0;
    } finally {
      process.removeListener("SIGINT", cancel);
      process.removeListener("SIGTERM", cancel);
    }
  }
  return main();
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  process.exitCode = await cli();
}
