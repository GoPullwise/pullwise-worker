import assert from "node:assert/strict";
import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  applyGatewayProfile,
  loadGatewayProfileState,
  parseGatewayManifestTrust,
} from "../../src/runtime/gateway-profile.ts";


function canonicalBytes(value: unknown): Buffer {
  function normalize(item: unknown): unknown {
    if (Array.isArray(item)) return item.map(normalize);
    if (item && typeof item === "object") {
      return Object.fromEntries(
        Object.entries(item as Record<string, unknown>)
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([key, child]) => [key, normalize(child)]),
      );
    }
    return item;
  }
  return Buffer.from(JSON.stringify(normalize(value)), "ascii");
}

function fixture(options: {
  tokenId?: string;
  expiresAt?: number;
  route?: Record<string, unknown>;
} = {}) {
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  const manifest = {
    schema_id: "pullwise-model-profile-set/v1",
    profile_set_id: "reviewer-production",
    revision: 12,
    routes: [{
      route_id: "gpt-primary",
      provider_connection_id: "openai-production",
      provider: "pullwise-gateway",
      model_alias: "gpt-reviewer",
      upstream_model: "gpt-5.5",
      api: "openai-completions",
      enabled: true,
      ...options.route,
    }],
  };
  const manifestBytes = canonicalBytes(manifest);
  const manifestDigest = createHash("sha256").update(manifestBytes).digest("hex");
  const publicKeyPem = publicKey.export({ type: "spki", format: "pem" }).toString();
  const publicKeyDer = publicKey.export({ type: "spki", format: "der" });
  const trust = parseGatewayManifestTrust({
    schema_id: "pullwise-model-gateway-manifest-trust/v1",
    alg: "Ed25519",
    kid: "gateway-signing-2026-09",
    public_key_pem: publicKeyPem,
    fingerprint: `sha256:${createHash("sha256").update(publicKeyDer).digest("hex")}`,
  });
  return {
    manifestDigest,
    trust,
    payload: {
      schema_id: "pullwise-worker-model-profile/v1",
      worker_id: "worker_a",
      worker_pool_id: "reviewers-primary",
      profile_set_id: "reviewer-production",
      profile_revision: 12,
      manifest,
      manifest_digest: manifestDigest,
      manifest_signature: {
        alg: "Ed25519",
        kid: "gateway-signing-2026-09",
        value: sign(
          null,
          Buffer.concat([
            Buffer.from("pullwise-model-profile-manifest/v1\0", "ascii"),
            manifestBytes,
          ]),
          privateKey,
        ).toString("base64url"),
      },
      gateway: {
        provider: "pullwise-gateway",
        base_url:
          "https://models.pull-wise.com/v1/workers/worker_a/profiles/reviewer-production/revisions/12",
      },
      authorization: {
        scheme: "Bearer",
        access_token: `gateway-token-${options.tokenId ?? "current"}`,
        expires_at: options.expiresAt ?? 1_788_259_500,
        jti: options.tokenId ?? "gtj_current",
      },
    },
  };
}

test("signed manifests still reject invalid route identity fields and expired grants", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-gateway-invalid-"));
  try {
    for (const route of [
      { provider_connection_id: "../provider" },
      { upstream_model: "" },
      { route_id: "invalid route" },
    ]) {
      const invalid = fixture({ route });
      await assert.rejects(
        applyGatewayProfile({
          profileRoot: root,
          expectedWorkerId: "worker_a",
          manifestPublicKeys: invalid.trust,
          payload: invalid.payload,
          clock: () => 1_788_259_201_000,
        }),
        /profile route|provider_connection_id|upstream_model|route_id/u,
      );
    }
    const expired = fixture({ expiresAt: 1_788_259_200 });
    await assert.rejects(
      applyGatewayProfile({
        profileRoot: root,
        expectedWorkerId: "worker_a",
        manifestPublicKeys: expired.trust,
        payload: expired.payload,
        clock: () => 1_788_259_201_000,
      }),
      /expired/u,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("an invalid pre-existing generation cannot replace the last good pointer", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-gateway-collision-"));
  try {
    const initial = fixture({ tokenId: "gtj_initial" });
    await applyGatewayProfile({
      profileRoot: root,
      expectedWorkerId: "worker_a",
      manifestPublicKeys: initial.trust,
      payload: initial.payload,
      clock: () => 1_788_259_201_000,
    });
    const pointerPath = join(root, "managed-current.json");
    const pointerBefore = await readFile(pointerPath, "utf8");
    const next = fixture({ tokenId: "gtj_next" });
    const collision = join(root, "generations", `${next.manifestDigest}.gtj_next`);
    await mkdir(join(root, "generations"), { recursive: true });
    await writeFile(collision, "not a generation directory", "utf8");

    await assert.rejects(
      applyGatewayProfile({
        profileRoot: root,
        expectedWorkerId: "worker_a",
        manifestPublicKeys: next.trust,
        payload: next.payload,
        clock: () => 1_788_259_201_000,
      }),
      /generation/u,
    );
    assert.equal(await readFile(pointerPath, "utf8"), pointerBefore);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("restart rejects a corrupted managed profile state instead of coercing it", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-gateway-restart-"));
  try {
    const current = fixture({ tokenId: "gtj_restart" });
    await applyGatewayProfile({
      profileRoot: root,
      expectedWorkerId: "worker_a",
      manifestPublicKeys: current.trust,
      payload: current.payload,
      clock: () => 1_788_259_201_000,
    });
    const pointer = JSON.parse(await readFile(join(root, "managed-current.json"), "utf8"));
    const statePath = join(root, "generations", pointer.generation, "profile-state.json");
    const state = JSON.parse(await readFile(statePath, "utf8"));
    await writeFile(
      statePath,
      JSON.stringify({ ...state, applied_revision: 0, worker_id: "../other-worker" }),
      "utf8",
    );

    await assert.rejects(loadGatewayProfileState(root), /gateway profile state/u);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
