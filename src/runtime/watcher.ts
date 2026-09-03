import type { HeartbeatState, RuntimeCatalog } from "./control-plane.ts";
import {
  buildRuntimeCatalog,
  loadProfiles,
  type Profile,
  type RuntimeModel,
} from "./profiles.ts";
import { readWorkerState } from "./worker-state.ts";
import type { GatewayProfileState } from "./gateway-profile.ts";

export interface WatcherClient {
  register(
    catalog: RuntimeCatalog,
    signal?: AbortSignal,
    profileState?: GatewayProfileState,
  ): Promise<unknown>;
  heartbeat(
    catalog: RuntimeCatalog,
    state: HeartbeatState,
    signal?: AbortSignal,
    profileState?: GatewayProfileState,
  ): Promise<unknown>;
}

export type WatcherCommand = Readonly<Record<string, unknown>>;

export interface WatcherSyncOptions {
  readonly profileRoot: string;
  readonly stateRoot: string;
  readonly client: WatcherClient;
  readonly register: boolean;
  readonly listModels?: (profile: Profile) => Promise<RuntimeModel[]>;
  readonly reconcileProfile?: (signal?: AbortSignal) => Promise<GatewayProfileState>;
  readonly signal?: AbortSignal;
  readonly handleCommand?: (command: WatcherCommand) => void | Promise<void>;
}

export async function syncWatcherOnce(options: WatcherSyncOptions): Promise<void> {
  const profileState = options.reconcileProfile
    ? await options.reconcileProfile(options.signal)
    : undefined;
  const profiles = await loadProfiles(options.profileRoot);
  const catalog = await buildRuntimeCatalog(profiles, options.listModels);
  let state: HeartbeatState;
  try {
    const stored = await readWorkerState(options.stateRoot);
    state = {
      status: stored.status as HeartbeatState["status"],
      activeRunId: stored.activeRunId,
      activeSessionId: stored.activeSessionId,
      progress: stored.progress,
      lastError: stored.lastError,
    };
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    state = {
      status: "degraded",
      activeRunId: null,
      activeSessionId: null,
      progress: null,
      lastError: `worker_state_unavailable: ${detail}`,
    };
  }
  if (options.register) await options.client.register(catalog, options.signal, profileState);
  const response = await options.client.heartbeat(catalog, state, options.signal, profileState);
  if (options.handleCommand && response && typeof response === "object") {
    const commands = (response as { commands?: unknown }).commands;
    if (Array.isArray(commands)) {
      for (const command of commands.slice(0, 16)) {
        if (command && typeof command === "object" && !Array.isArray(command)) {
          await options.handleCommand(command as WatcherCommand);
        }
      }
    }
  }
}

function waitForNextSync(milliseconds: number, signal: AbortSignal): Promise<void> {
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

export interface WatcherLoopOptions extends Omit<WatcherSyncOptions, "register" | "signal"> {
  readonly intervalMs?: number;
  readonly signal: AbortSignal;
  readonly onError?: (error: Error) => void;
}

export async function runWatcher(options: WatcherLoopOptions): Promise<void> {
  const intervalMs = Math.max(1_000, options.intervalMs ?? 15_000);
  let registered = false;
  while (!options.signal.aborted) {
    try {
      await syncWatcherOnce({
        ...options,
        register: !registered,
        signal: options.signal,
      });
      registered = true;
    } catch (error) {
      options.onError?.(error instanceof Error ? error : new Error(String(error)));
    }
    await waitForNextSync(intervalMs, options.signal);
  }
}
