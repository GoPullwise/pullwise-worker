import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  applyGatewayProfile,
  loadGatewayProfileState,
  parseGatewayManifestTrust,
} from "../../src/runtime/gateway-profile.ts";
import { buildRuntimeCatalog, loadProfiles } from "../../src/runtime/profiles.ts";

function canonicalBytes(value: unknown): Buffer {
  function normalize(item: unknown): unknown {
    if (Array.isArray(item)) return item.map(normalize);
    if (item && typeof item === "object") {
      return Object.fromEntries(
        Object.entries(item as Record<string, unknown>)
          .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
          .map(([key, child]) => [key, normalize(child)]),
      );
    }
    return item;
  }
  return Buffer.from(JSON.stringify(normalize(value)), "ascii");
}

test("signed gateway profile atomically becomes the Pi runtime catalog", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-gateway-profile-"));
  try {
    const { privateKey, publicKey } = generateKeyPairSync("ed25519");
    const manifest = {
      schema_id: "pullwise-model-profile-set/v1",
      profile_set_id: "reviewer-production",
      revision: 12,
      routes: [
        {
          route_id: "gpt-primary",
          provider_connection_id: "openai-production",
          provider: "pullwise-gateway",
          model_alias: "gpt-reviewer",
          upstream_model: "gpt-5.5",
          api: "openai-completions",
          enabled: true,
        },
      ],
    };
    const manifestBytes = canonicalBytes(manifest);
    const manifestDigest = (await import("node:crypto"))
      .createHash("sha256")
      .update(manifestBytes)
      .digest("hex");
    const signature = sign(
      null,
      Buffer.concat([
        Buffer.from("pullwise-model-profile-manifest/v1\0", "ascii"),
        manifestBytes,
      ]),
      privateKey,
    ).toString("base64url");
    const accessToken = "gateway-token-worker-a-short-lived";
    const publicKeyPem = publicKey.export({ type: "spki", format: "pem" }).toString();
    const publicKeyDer = publicKey.export({ type: "spki", format: "der" });
    const manifestPublicKeys = parseGatewayManifestTrust({
      schema_id: "pullwise-model-gateway-manifest-trust/v1",
      alg: "Ed25519",
      kid: "gateway-signing-2026-09",
      public_key_pem: publicKeyPem,
      fingerprint: "sha256:" + (await import("node:crypto")).createHash("sha256").update(publicKeyDer).digest("hex"),
    });

    const applied = await applyGatewayProfile({
      profileRoot: root,
      expectedWorkerId: "worker_a",
      manifestPublicKeys,
      clock: () => 1_788_259_201_000,
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
          value: signature,
        },
        gateway: {
          provider: "pullwise-gateway",
          base_url:
            "https://models.pull-wise.com/v1/workers/worker_a/profiles/reviewer-production/revisions/12",
        },
        authorization: {
          scheme: "Bearer",
          access_token: accessToken,
          expires_at: 1_788_259_500,
          jti: "gtj_worker_a",
        },
      },
    });

    assert.equal(applied.appliedRevision, 12);
    assert.equal(applied.manifestDigest, manifestDigest);
    const profiles = await loadProfiles(root);
    const catalog = await buildRuntimeCatalog(profiles);
    assert.deepEqual(catalog, {
      schema_id: "pullwise-pi-runtime-catalog/v1",
      credentials: [
        {
          credential_id: "gateway-reviewer-production",
          label: "reviewer-production",
          provider: "pullwise-gateway",
          auth_type: "api_key",
          models: [{ id: "gpt-reviewer", name: "gpt-reviewer" }],
        },
      ],
    });

    const pointer = JSON.parse(await readFile(join(root, "managed-current.json"), "utf8"));
    assert.equal(pointer.manifest_digest, manifestDigest);
    assert.equal(pointer.generation, `${manifestDigest}.gtj_worker_a`);
    const generationRoot = join(root, "generations", pointer.generation);
    const agentDir = join(generationRoot, "profiles", "gateway-reviewer-production");
    const authText = await readFile(join(agentDir, "auth.json"), "utf8");
    const modelsText = await readFile(join(agentDir, "models.json"), "utf8");
    const profilesText = await readFile(join(generationRoot, "profiles.json"), "utf8");
    const stateText = await readFile(join(generationRoot, "profile-state.json"), "utf8");
    assert.match(authText, new RegExp(accessToken, "u"));
    assert.doesNotMatch(modelsText, new RegExp(accessToken, "u"));
    assert.doesNotMatch(profilesText, new RegExp(accessToken, "u"));
    assert.doesNotMatch(stateText, new RegExp(accessToken, "u"));
    assert.deepEqual(await loadGatewayProfileState(root), applied);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
