import { createHash, createPublicKey, verify, type KeyLike } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";

import {
  privateProfileRoot,
  publishGatewayProfileGeneration,
} from "./gateway-profile-files.ts";
import { parseStrictJson } from "./strict-json.ts";

const SIGNATURE_PREFIX = Buffer.from("pullwise-model-profile-manifest/v1\0", "ascii");
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const PROFILE_SET_ID = /^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const PAYLOAD_KEYS = [
  "authorization", "gateway", "manifest", "manifest_digest", "manifest_signature",
  "profile_revision", "profile_set_id", "schema_id", "worker_id", "worker_pool_id",
];
const MANIFEST_KEYS = ["profile_set_id", "revision", "routes", "schema_id"];
const ROUTE_KEYS = [
  "api", "enabled", "model_alias", "provider", "provider_connection_id", "route_id",
  "upstream_model",
];

export interface GatewayProfileState {
  readonly schemaId: "pullwise-worker-profile-state/v1";
  readonly workerId: string;
  readonly workerPoolId: string;
  readonly profileSetId: string;
  readonly desiredRevision: number;
  readonly appliedRevision: number;
  readonly manifestDigest: string;
  readonly catalogDigest: string;
  readonly gatewayTokenExpiresAt: number;
  readonly gatewayTokenId: string;
  readonly lastApplyResult: "succeeded";
  readonly appliedAt: number;
}

interface ApplyGatewayProfileOptions {
  readonly profileRoot: string;
  readonly expectedWorkerId: string;
  readonly manifestPublicKeys: Readonly<Record<string, string | Buffer | KeyLike>>;
  readonly payload: unknown;
  readonly clock?: () => number;
}

function closedObject(value: unknown, keys: readonly string[], label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  const record = value as Record<string, unknown>;
  if (JSON.stringify(Object.keys(record).sort()) !== JSON.stringify([...keys].sort())) {
    throw new TypeError(`${label} must be a closed object`);
  }
  return record;
}

function requiredId(value: unknown, label: string): string {
  if (typeof value !== "string" || !SAFE_ID.test(value)) throw new TypeError(`${label} is invalid`);
  return value;
}

function positiveInteger(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || Number(value) <= 0) throw new TypeError(`${label} is invalid`);
  return Number(value);
}

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
        .map(([key, child]) => [key, canonicalValue(child)]),
    );
  }
  return value;
}

function canonicalBytes(value: unknown): Buffer {
  return Buffer.from(JSON.stringify(canonicalValue(value)), "ascii");
}

function decodedSignature(value: unknown): Buffer {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/u.test(value)) {
    throw new TypeError("manifest signature is invalid");
  }
  const decoded = Buffer.from(value, "base64url");
  if (decoded.toString("base64url") !== value || decoded.length !== 64) {
    throw new TypeError("manifest signature is invalid");
  }
  return decoded;
}

function scopedGatewayUrl(value: unknown, workerId: string, profileSetId: string, revision: number): string {
  if (typeof value !== "string") throw new TypeError("gateway base_url is invalid");
  const url = new URL(value);
  const expectedPath = `/v1/workers/${encodeURIComponent(workerId)}/profiles/${encodeURIComponent(profileSetId)}/revisions/${revision}`;
  if (
    url.protocol !== "https:" || url.username || url.password || url.search || url.hash ||
    url.pathname !== expectedPath
  ) {
    throw new TypeError("gateway base_url is not bound to this Worker profile");
  }
  return url.href.replace(/\/$/u, "");
}

function profileCredentialId(profileSetId: string): string {
  const readable = `gateway-${profileSetId}`;
  return readable.length <= 64 ? readable : `gateway-${createHash("sha256").update(profileSetId).digest("hex").slice(0, 32)}`;
}

export function parseGatewayManifestTrust(value: unknown): Readonly<Record<string, KeyLike>> {
  const trust = closedObject(
    value,
    ["alg", "fingerprint", "kid", "public_key_pem", "schema_id"],
    "gateway manifest trust",
  );
  if (
    trust.schema_id !== "pullwise-model-gateway-manifest-trust/v1" ||
    trust.alg !== "Ed25519"
  ) {
    throw new TypeError("gateway manifest trust schema or algorithm is invalid");
  }
  const keyId = requiredId(trust.kid, "gateway manifest trust key id");
  if (typeof trust.public_key_pem !== "string" || !trust.public_key_pem.includes("BEGIN PUBLIC KEY")) {
    throw new TypeError("gateway manifest public key is invalid");
  }
  const key = createPublicKey(trust.public_key_pem);
  if (key.asymmetricKeyType !== "ed25519") {
    throw new TypeError("gateway manifest public key must be Ed25519");
  }
  const fingerprint = `sha256:${createHash("sha256")
    .update(key.export({ type: "spki", format: "der" }))
    .digest("hex")}`;
  if (trust.fingerprint !== fingerprint) {
    throw new Error("gateway manifest public key fingerprint mismatch");
  }
  return Object.freeze({ [keyId]: key });
}

export async function applyGatewayProfile(options: ApplyGatewayProfileOptions): Promise<GatewayProfileState> {
  const payload = closedObject(options.payload, PAYLOAD_KEYS, "gateway profile payload");
  if (payload.schema_id !== "pullwise-worker-model-profile/v1") throw new TypeError("gateway profile schema is invalid");
  const workerId = requiredId(payload.worker_id, "worker_id");
  if (workerId !== options.expectedWorkerId) throw new Error("gateway profile does not match this Worker");
  const workerPoolId = requiredId(payload.worker_pool_id, "worker_pool_id");
  const profileSetId = requiredId(payload.profile_set_id, "profile_set_id");
  if (!PROFILE_SET_ID.test(profileSetId)) throw new TypeError("profile_set_id is invalid");
  const revision = positiveInteger(payload.profile_revision, "profile_revision");
  const manifest = closedObject(payload.manifest, MANIFEST_KEYS, "profile manifest");
  if (
    manifest.schema_id !== "pullwise-model-profile-set/v1" ||
    manifest.profile_set_id !== profileSetId ||
    manifest.revision !== revision ||
    !Array.isArray(manifest.routes) ||
    manifest.routes.length < 1 ||
    manifest.routes.length > 32
  ) throw new TypeError("profile manifest binding is invalid");
  const manifestBytes = canonicalBytes(manifest);
  const digest = createHash("sha256").update(manifestBytes).digest("hex");
  if (!SHA256.test(String(payload.manifest_digest)) || payload.manifest_digest !== digest) {
    throw new Error("profile manifest digest mismatch");
  }
  const signature = closedObject(payload.manifest_signature, ["alg", "kid", "value"], "manifest signature");
  const keyId = requiredId(signature.kid, "manifest signature key id");
  const publicKey = options.manifestPublicKeys[keyId];
  if (signature.alg !== "Ed25519" || !publicKey) throw new Error("manifest signature key is unavailable");
  if (!verify(null, Buffer.concat([SIGNATURE_PREFIX, manifestBytes]), publicKey, decodedSignature(signature.value))) {
    throw new Error("profile manifest signature is invalid");
  }
  const gateway = closedObject(payload.gateway, ["base_url", "provider"], "gateway configuration");
  if (gateway.provider !== "pullwise-gateway") throw new TypeError("gateway provider is invalid");
  const baseUrl = scopedGatewayUrl(gateway.base_url, workerId, profileSetId, revision);
  const authorization = closedObject(payload.authorization, ["access_token", "expires_at", "jti", "scheme"], "gateway authorization");
  if (authorization.scheme !== "Bearer") throw new TypeError("gateway authorization scheme is invalid");
  const accessToken = typeof authorization.access_token === "string" ? authorization.access_token : "";
  if (!accessToken || accessToken.length > 16_384 || /[\u0000-\u001f\u007f]/u.test(accessToken)) {
    throw new TypeError("gateway access token is invalid");
  }
  const expiresAt = positiveInteger(authorization.expires_at, "gateway token expiry");
  const tokenId = requiredId(authorization.jti, "gateway token id");
  const clock = options.clock ?? Date.now;
  if (expiresAt * 1000 <= clock()) throw new Error("gateway access token is expired");
  const routes = manifest.routes.map((route, index) => {
    const item = closedObject(route, ROUTE_KEYS, `profile route ${index}`);
    if (
      item.provider !== "pullwise-gateway" || item.api !== "openai-completions" ||
      typeof item.enabled !== "boolean"
    ) throw new TypeError("profile route is invalid");
    return {
      routeId: requiredId(item.route_id, "route_id"),
      providerConnectionId: requiredId(item.provider_connection_id, "provider_connection_id"),
      modelAlias: requiredId(item.model_alias, "model_alias"),
      upstreamModel: requiredId(item.upstream_model, "upstream_model"),
      enabled: item.enabled,
    };
  });
  const enabledModels = routes.filter((route) => route.enabled).map((route) => ({
    id: route.modelAlias,
    name: route.modelAlias,
    reasoning: true,
  }));
  if (!enabledModels.length || new Set(enabledModels.map((model) => model.id)).size !== enabledModels.length) {
    throw new TypeError("profile manifest must contain unique enabled model aliases");
  }
  const credentialId = profileCredentialId(profileSetId);
  const catalogShape = {
    credential_id: credentialId,
    provider: "pullwise-gateway",
    models: enabledModels.map(({ id, name }) => ({ id, name })),
  };
  const state: GatewayProfileState = Object.freeze({
    schemaId: "pullwise-worker-profile-state/v1",
    workerId,
    workerPoolId,
    profileSetId,
    desiredRevision: revision,
    appliedRevision: revision,
    manifestDigest: digest,
    catalogDigest: createHash("sha256").update(canonicalBytes(catalogShape)).digest("hex"),
    gatewayTokenExpiresAt: expiresAt,
    gatewayTokenId: tokenId,
    lastApplyResult: "succeeded",
    appliedAt: Math.floor(clock() / 1000),
  });

  const generation = `${digest}.${tokenId}`;
  await publishGatewayProfileGeneration({
    profileRoot: options.profileRoot,
    generation,
    credentialId,
    profileSetId,
    accessToken,
    baseUrl,
    enabledModels,
    state,
  });
  return state;
}

export async function loadGatewayProfileState(profileRoot: string): Promise<GatewayProfileState> {
  const root = await privateProfileRoot(profileRoot);
  const pointer = closedObject(
    parseStrictJson(await readFile(path.join(root, "managed-current.json"), "utf8")),
    ["generation", "manifest_digest", "schema_id"],
    "managed profile pointer",
  );
  const generation = String(pointer?.generation ?? "");
  if (
    pointer.schema_id !== "pullwise-managed-profile-pointer/v1" ||
    !/^[0-9a-f]{64}\.[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u.test(generation) ||
    pointer.manifest_digest !== generation.slice(0, 64)
  ) {
    throw new TypeError("managed profile pointer is invalid");
  }
  const raw = parseStrictJson(await readFile(path.join(root, "generations", generation, "profile-state.json"), "utf8"));
  const state = closedObject(raw, [
    "applied_at", "applied_revision", "catalog_digest", "desired_revision",
    "gateway_token_expires_at", "gateway_token_id", "last_apply_result",
    "manifest_digest", "profile_set_id", "schema_id", "worker_id", "worker_pool_id",
  ], "gateway profile state");
  if (state.schema_id !== "pullwise-worker-profile-state/v1" || state.last_apply_result !== "succeeded") {
    throw new TypeError("gateway profile state is invalid");
  }
  const desiredRevision = Number(state.desired_revision);
  const appliedRevision = Number(state.applied_revision);
  if (
    typeof state.worker_id !== "string" || !SAFE_ID.test(state.worker_id) ||
    typeof state.worker_pool_id !== "string" || !SAFE_ID.test(state.worker_pool_id) ||
    typeof state.profile_set_id !== "string" || !PROFILE_SET_ID.test(state.profile_set_id) ||
    !Number.isSafeInteger(desiredRevision) || desiredRevision <= 0 ||
    appliedRevision !== desiredRevision ||
    typeof state.manifest_digest !== "string" || !SHA256.test(state.manifest_digest) ||
    state.manifest_digest !== pointer.manifest_digest ||
    typeof state.catalog_digest !== "string" || !SHA256.test(state.catalog_digest) ||
    !Number.isSafeInteger(state.gateway_token_expires_at) || Number(state.gateway_token_expires_at) <= 0 ||
    typeof state.gateway_token_id !== "string" || !SAFE_ID.test(state.gateway_token_id) ||
    !Number.isSafeInteger(state.applied_at) || Number(state.applied_at) <= 0
  ) throw new TypeError("gateway profile state is invalid");
  return Object.freeze({
    schemaId: state.schema_id,
    workerId: state.worker_id,
    workerPoolId: state.worker_pool_id,
    profileSetId: state.profile_set_id,
    desiredRevision,
    appliedRevision,
    manifestDigest: state.manifest_digest,
    catalogDigest: state.catalog_digest,
    gatewayTokenExpiresAt: Number(state.gateway_token_expires_at),
    gatewayTokenId: state.gateway_token_id,
    lastApplyResult: "succeeded",
    appliedAt: Number(state.applied_at),
  });
}
