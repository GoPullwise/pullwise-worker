import { lstat, mkdir, readFile, realpath } from "node:fs/promises";
import path from "node:path";
import { TextDecoder } from "node:util";

import { ModelRuntime } from "@earendil-works/pi-coding-agent";

import { parseStrictJson } from "./strict-json.ts";

const PROFILE_SCHEMA_ID = "pullwise-pi-profiles/v1";
const CATALOG_SCHEMA_ID = "pullwise-pi-runtime-catalog/v1";
const PROFILE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/u;
const PROVIDER_ID = /^[a-z0-9][a-z0-9._-]{0,59}$/u;
const REPARSE_POINT = 0x400;
const MANAGED_GENERATION = /^[0-9a-f]{64}\.[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;

export interface ProfileInput {
  readonly credentialId: string;
  readonly label: string;
  readonly provider: string;
  readonly authType?: "api_key" | "oauth" | "subscription";
}

export interface Profile extends ProfileInput {
  readonly authType: "api_key" | "oauth" | "subscription";
  readonly agentDir: string;
}

export interface Profiles {
  readonly root: string;
  readonly profiles: readonly Profile[];
}

export interface RuntimeModel {
  readonly id: string;
  readonly name: string;
}

function safeText(value: unknown, label: string, maxLength: number): string {
  const text = String(value ?? "").trim();
  if (!text || text.length > maxLength || [...text].some((character) => character.charCodeAt(0) < 32)) {
    throw new TypeError(`${label} is invalid`);
  }
  return text;
}

async function safeRoot(root: string): Promise<string> {
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
    throw new Error("profile root must be a real, non-linked directory");
  }
  return resolved;
}

async function currentProfileRoot(root: string): Promise<string> {
  const pointerPath = path.join(root, "managed-current.json");
  let pointerText: string;
  try {
    const metadata = await lstat(pointerPath);
    const resolvedPointer = await realpath(pointerPath);
    if (!metadata.isFile() || metadata.isSymbolicLink() || path.dirname(resolvedPointer) !== root) {
      throw new Error("managed profile pointer must be a regular file inside the profile root");
    }
    pointerText = new TextDecoder("utf-8", { fatal: true }).decode(await readFile(resolvedPointer));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      throw new Error("managed profile pointer is required");
    }
    throw error;
  }
  const parsed = parseStrictJson(pointerText);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new TypeError("managed profile pointer must be an object");
  }
  const pointer = parsed as Record<string, unknown>;
  if (JSON.stringify(Object.keys(pointer).sort()) !== JSON.stringify([
    "generation",
    "manifest_digest",
    "schema_id",
  ])) {
    throw new TypeError("managed profile pointer must be a closed object");
  }
  const generation = String(pointer.generation ?? "");
  if (
    pointer.schema_id !== "pullwise-managed-profile-pointer/v1" ||
    !MANAGED_GENERATION.test(generation) ||
    String(pointer.manifest_digest ?? "") !== generation.slice(0, 64)
  ) {
    throw new TypeError("managed profile pointer is invalid");
  }
  const generationRoot = path.join(root, "generations", generation);
  const metadata = await lstat(generationRoot);
  const resolved = await realpath(generationRoot);
  if (
    !metadata.isDirectory() ||
    metadata.isSymbolicLink() ||
    path.dirname(resolved) !== path.join(root, "generations") ||
    path.basename(resolved) !== generation
  ) {
    throw new Error("managed profile generation must be a real contained directory");
  }
  return resolved;
}

function normalizeInput(input: ProfileInput): ProfileInput {
  const credentialId = safeText(input.credentialId, "credentialId", 64);
  const provider = safeText(input.provider, "provider", 60).toLowerCase();
  if (!PROFILE_ID.test(credentialId) || !PROVIDER_ID.test(provider)) {
    throw new TypeError("credentialId or provider is invalid");
  }
  const authType = input.authType ?? "api_key";
  if (!(["api_key", "oauth", "subscription"] as const).includes(authType)) {
    throw new TypeError("authType is invalid");
  }
  return { credentialId, provider, label: safeText(input.label, "label", 120), authType };
}

export async function loadProfiles(root: string): Promise<Profiles> {
  const profileRoot = await safeRoot(root);
  const resolvedRoot = await currentProfileRoot(profileRoot);
  const configPath = path.join(resolvedRoot, "profiles.json");
  let text: string;
  try {
    const metadata = await lstat(configPath);
    const resolvedConfig = await realpath(configPath);
    if (!metadata.isFile() || metadata.isSymbolicLink() || path.dirname(resolvedConfig) !== resolvedRoot) {
      throw new Error("profile config must be a regular file inside the profile root");
    }
    text = new TextDecoder("utf-8", { fatal: true }).decode(await readFile(resolvedConfig));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return Object.freeze({ root: resolvedRoot, profiles: Object.freeze([]) });
    }
    throw error;
  }
  const parsed = parseStrictJson(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new TypeError("profile config must be an object");
  }
  const config = parsed as Record<string, unknown>;
  if (JSON.stringify(Object.keys(config).sort()) !== JSON.stringify(["profiles", "schema_id"])) {
    throw new TypeError("profile config must be a closed object");
  }
  if (config.schema_id !== PROFILE_SCHEMA_ID || !Array.isArray(config.profiles) || config.profiles.length > 32) {
    throw new TypeError("profile config schema or profile count is invalid");
  }
  const ids = new Set<string>();
  const profiles: Profile[] = [];
  for (const raw of config.profiles) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      throw new TypeError("profile must be a closed metadata object");
    }
    const item = raw as Record<string, unknown>;
    if (JSON.stringify(Object.keys(item).sort()) !== JSON.stringify([
      "agent_dir",
      "auth_type",
      "credential_id",
      "label",
      "provider",
    ])) {
      throw new TypeError("profile must be a closed metadata object");
    }
    if (!["api_key", "oauth", "subscription"].includes(String(item.auth_type))) {
      throw new TypeError("profile auth_type is invalid");
    }
    const normalized = normalizeInput({
      credentialId: String(item.credential_id ?? ""),
      label: String(item.label ?? ""),
      provider: String(item.provider ?? ""),
      authType: item.auth_type as Profile["authType"],
    });
    if (ids.has(normalized.credentialId)) throw new TypeError("credentialId values must be unique");
    ids.add(normalized.credentialId);
    const relativeAgentDir = String(item.agent_dir ?? "");
    const agentDir = path.resolve(resolvedRoot, relativeAgentDir);
    if (
      path.isAbsolute(relativeAgentDir) ||
      path.relative(resolvedRoot, agentDir).startsWith("..") ||
      relativeAgentDir.replaceAll("\\", "/") !== `profiles/${normalized.credentialId}`
    ) {
      throw new TypeError("profile agent_dir is invalid");
    }
    await mkdir(agentDir, { recursive: true, mode: 0o700 });
    profiles.push(Object.freeze({ ...normalized, authType: normalized.authType ?? "api_key", agentDir }));
  }
  return Object.freeze({ root: resolvedRoot, profiles: Object.freeze(profiles) });
}

async function listPiModels(profile: Profile): Promise<RuntimeModel[]> {
  const runtime = await ModelRuntime.create({
    authPath: path.join(profile.agentDir, "auth.json"),
    modelsPath: path.join(profile.agentDir, "models.json"),
    allowModelNetwork: false,
    refreshOnCreate: false,
  });
  const models = await runtime.getAvailable(profile.provider);
  return models.map((model) => ({ id: model.id, name: model.name || model.id }));
}

export async function buildRuntimeCatalog(
  profiles: Profiles,
  listModels: (profile: Profile) => Promise<RuntimeModel[]> = listPiModels,
) {
  const credentials = [];
  for (const profile of profiles.profiles) {
    const models = await listModels(profile);
    if (!models.length) continue;
    credentials.push({
      credential_id: profile.credentialId,
      label: profile.label,
      provider: profile.provider,
      auth_type: profile.authType,
      models: models.map((model) => ({ id: model.id, name: model.name || model.id })),
    });
  }
  return { schema_id: CATALOG_SCHEMA_ID, credentials } as const;
}
