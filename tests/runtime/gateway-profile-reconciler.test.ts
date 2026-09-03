import assert from "node:assert/strict";
import test from "node:test";

import { createGatewayProfileReconciler } from "../../src/runtime/gateway-profile-reconciler.ts";
import type { GatewayProfileState } from "../../src/runtime/gateway-profile.ts";

function state(tokenId: string, expiresAt: number): GatewayProfileState {
  return {
    schemaId: "pullwise-worker-profile-state/v1",
    workerId: "worker_a",
    workerPoolId: "reviewers-primary",
    profileSetId: "reviewer-production",
    desiredRevision: 12,
    appliedRevision: 12,
    manifestDigest: "a".repeat(64),
    catalogDigest: "b".repeat(64),
    gatewayTokenExpiresAt: expiresAt,
    gatewayTokenId: tokenId,
    lastApplyResult: "succeeded",
    appliedAt: 1_788_259_200,
  };
}

test("profile reconciler rate-limits pulls and refreshes before token expiry", async () => {
  let now = 1_788_259_200_000;
  let fetches = 0;
  const applied: unknown[] = [];
  const reconciler = createGatewayProfileReconciler({
    client: {
      fetchModelProfile: async () => ({ generation: ++fetches }),
    },
    applyProfile: async (payload) => {
      applied.push(payload);
      return fetches === 1
        ? state("gtj_first", Math.floor(now / 1000) + 300)
        : state("gtj_second", Math.floor(now / 1000) + 300);
    },
    loadState: async () => { throw Object.assign(new Error("missing"), { code: "ENOENT" }); },
    clock: () => now,
    checkIntervalMs: 60_000,
    refreshBeforeExpiryMs: 60_000,
  });

  const first = await reconciler();
  now += 30_000;
  const cached = await reconciler();
  now += 211_000;
  const refreshed = await reconciler();

  assert.equal(first.gatewayTokenId, "gtj_first");
  assert.equal(cached.gatewayTokenId, "gtj_first");
  assert.equal(refreshed.gatewayTokenId, "gtj_second");
  assert.equal(fetches, 2);
  assert.deepEqual(applied, [{ generation: 1 }, { generation: 2 }]);
});

test("concurrent reconciliation shares one pull and network loss fails at the safety margin", async () => {
  let now = 1_788_259_200_000;
  let fetches = 0;
  let release: (() => void) | undefined;
  const fetchGate = new Promise<void>((resolve) => { release = resolve; });
  const current = state("gtj_current", Math.floor(now / 1000) + 90);
  const reconciler = createGatewayProfileReconciler({
    client: {
      fetchModelProfile: async () => {
        fetches += 1;
        await fetchGate;
        throw new Error("control plane unavailable");
      },
    },
    applyProfile: async () => { throw new Error("must not apply"); },
    loadState: async () => current,
    clock: () => now,
    checkIntervalMs: 60_000,
    refreshBeforeExpiryMs: 60_000,
    failureSafetyMarginMs: 30_000,
  });

  const first = reconciler();
  const second = reconciler();
  assert.equal(first, second);
  release?.();
  assert.equal(await first, current);
  assert.equal(fetches, 1);

  now += 61_000;
  await assert.rejects(reconciler(), /control plane unavailable/u);
  assert.equal(fetches, 2);
});
