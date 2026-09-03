import type { GatewayProfileState } from "./gateway-profile.ts";


interface ModelProfileClient {
  fetchModelProfile(signal?: AbortSignal): Promise<unknown>;
}

interface GatewayProfileReconcilerOptions {
  readonly client: ModelProfileClient;
  readonly applyProfile: (payload: unknown, signal?: AbortSignal) => Promise<GatewayProfileState>;
  readonly loadState: () => Promise<GatewayProfileState>;
  readonly clock?: () => number;
  readonly checkIntervalMs?: number;
  readonly refreshBeforeExpiryMs?: number;
  readonly failureSafetyMarginMs?: number;
}

function positiveMilliseconds(value: number, label: string): number {
  if (!Number.isSafeInteger(value) || value <= 0) throw new TypeError(`${label} must be positive`);
  return value;
}

export function createGatewayProfileReconciler(options: GatewayProfileReconcilerOptions) {
  const clock = options.clock ?? Date.now;
  const checkIntervalMs = positiveMilliseconds(options.checkIntervalMs ?? 60_000, "checkIntervalMs");
  const refreshBeforeExpiryMs = positiveMilliseconds(
    options.refreshBeforeExpiryMs ?? 60_000,
    "refreshBeforeExpiryMs",
  );
  const failureSafetyMarginMs = positiveMilliseconds(
    options.failureSafetyMarginMs ?? 30_000,
    "failureSafetyMarginMs",
  );
  let current: GatewayProfileState | undefined;
  let initialStateLoaded = false;
  let nextCheckAt = 0;
  let inFlight: Promise<GatewayProfileState> | undefined;

  async function loadInitialState(): Promise<void> {
    if (initialStateLoaded) return;
    initialStateLoaded = true;
    try {
      current = await options.loadState();
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }

  async function perform(signal?: AbortSignal): Promise<GatewayProfileState> {
    await loadInitialState();
    const now = clock();
    if (current && now < nextCheckAt) return current;
    try {
      const payload = await options.client.fetchModelProfile(signal);
      current = await options.applyProfile(payload, signal);
    } catch (error) {
      if (current && current.gatewayTokenExpiresAt * 1000 > now + failureSafetyMarginMs) {
        nextCheckAt = Math.min(
          now + checkIntervalMs,
          current.gatewayTokenExpiresAt * 1000 - failureSafetyMarginMs,
        );
        return current;
      }
      throw error;
    }
    nextCheckAt = Math.min(
      now + checkIntervalMs,
      current.gatewayTokenExpiresAt * 1000 - refreshBeforeExpiryMs,
    );
    return current;
  }

  return function reconcile(signal?: AbortSignal): Promise<GatewayProfileState> {
    if (inFlight) return inFlight;
    inFlight = perform(signal).finally(() => {
      inFlight = undefined;
    });
    return inFlight;
  };
}
