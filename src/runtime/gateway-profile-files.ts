import { randomBytes } from "node:crypto";
import { lstat, mkdir, open, readFile, realpath, rename, rm } from "node:fs/promises";
import path from "node:path";

import type { GatewayProfileState } from "./gateway-profile.ts";


interface GatewayModel {
  readonly id: string;
  readonly name: string;
  readonly reasoning: boolean;
}

interface PublishOptions {
  readonly profileRoot: string;
  readonly generation: string;
  readonly credentialId: string;
  readonly profileSetId: string;
  readonly accessToken: string;
  readonly baseUrl: string;
  readonly enabledModels: readonly GatewayModel[];
  readonly state: GatewayProfileState;
}

export async function privateProfileRoot(value: string): Promise<string> {
  const lexical = path.resolve(value);
  await mkdir(lexical, { recursive: true, mode: 0o700 });
  const resolved = await realpath(lexical);
  const metadata = await lstat(resolved);
  if (!metadata.isDirectory() || metadata.isSymbolicLink() || resolved !== lexical) {
    throw new Error("profile root must be a real directory");
  }
  return resolved;
}

async function writePrivateFile(filePath: string, value: unknown): Promise<void> {
  const handle = await open(filePath, "wx", 0o600);
  try {
    await handle.writeFile(`${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8" });
    await handle.sync();
  } finally {
    await handle.close();
  }
}

async function syncDirectory(directory: string): Promise<void> {
  if (process.platform === "win32") return;
  const handle = await open(directory, "r");
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}

async function existingGenerationMatches(
  stageRoot: string,
  finalRoot: string,
  credentialId: string,
): Promise<boolean> {
  try {
    const finalMetadata = await lstat(finalRoot);
    const resolvedFinal = await realpath(finalRoot);
    if (
      !finalMetadata.isDirectory() ||
      finalMetadata.isSymbolicLink() ||
      path.dirname(resolvedFinal) !== path.dirname(finalRoot)
    ) return false;
    for (const relative of [
      "profiles.json",
      "profile-state.json",
      `profiles/${credentialId}/auth.json`,
      `profiles/${credentialId}/models.json`,
    ]) {
      const expectedPath = path.join(stageRoot, relative);
      const existingPath = path.join(finalRoot, relative);
      const metadata = await lstat(existingPath);
      const resolved = await realpath(existingPath);
      if (
        !metadata.isFile() ||
        metadata.isSymbolicLink() ||
        path.relative(resolvedFinal, resolved).startsWith("..") ||
        !(await readFile(expectedPath)).equals(await readFile(existingPath))
      ) return false;
    }
    return true;
  } catch {
    return false;
  }
}

export async function publishGatewayProfileGeneration(options: PublishOptions): Promise<void> {
  const root = await privateProfileRoot(options.profileRoot);
  const generationsRoot = path.join(root, "generations");
  await mkdir(generationsRoot, { recursive: true, mode: 0o700 });
  const finalRoot = path.join(generationsRoot, options.generation);
  const stageRoot = path.join(generationsRoot, `.stage-${randomBytes(12).toString("hex")}`);
  try {
    await mkdir(path.join(stageRoot, "profiles", options.credentialId), { recursive: true, mode: 0o700 });
    await writePrivateFile(path.join(stageRoot, "profiles.json"), {
      schema_id: "pullwise-pi-profiles/v1",
      profiles: [{
        credential_id: options.credentialId,
        label: options.profileSetId,
        provider: "pullwise-gateway",
        auth_type: "api_key",
        agent_dir: `profiles/${options.credentialId}`,
      }],
    });
    await writePrivateFile(path.join(stageRoot, "profiles", options.credentialId, "auth.json"), {
      "pullwise-gateway": { type: "api_key", key: options.accessToken },
    });
    await writePrivateFile(path.join(stageRoot, "profiles", options.credentialId, "models.json"), {
      providers: {
        "pullwise-gateway": {
          baseUrl: options.baseUrl,
          api: "openai-completions",
          authHeader: true,
          models: options.enabledModels,
        },
      },
    });
    await writePrivateFile(path.join(stageRoot, "profile-state.json"), {
      schema_id: options.state.schemaId,
      worker_id: options.state.workerId,
      worker_pool_id: options.state.workerPoolId,
      profile_set_id: options.state.profileSetId,
      desired_revision: options.state.desiredRevision,
      applied_revision: options.state.appliedRevision,
      manifest_digest: options.state.manifestDigest,
      catalog_digest: options.state.catalogDigest,
      gateway_token_expires_at: options.state.gatewayTokenExpiresAt,
      gateway_token_id: options.state.gatewayTokenId,
      last_apply_result: options.state.lastApplyResult,
      applied_at: options.state.appliedAt,
    });
    let finalExists = true;
    try {
      await lstat(finalRoot);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      finalExists = false;
    }
    if (finalExists) {
      if (!await existingGenerationMatches(stageRoot, finalRoot, options.credentialId)) {
        throw new Error("existing managed profile generation is invalid");
      }
    } else {
      try {
        await rename(stageRoot, finalRoot);
        await syncDirectory(generationsRoot);
      } catch (error) {
        if (!["EEXIST", "ENOTEMPTY"].includes((error as NodeJS.ErrnoException).code ?? "")) throw error;
        if (!await existingGenerationMatches(stageRoot, finalRoot, options.credentialId)) {
          throw new Error("existing managed profile generation is invalid");
        }
      }
    }
  } finally {
    await rm(stageRoot, { recursive: true, force: true });
  }
  const pointerPath = path.join(root, `.managed-current-${process.pid}-${randomBytes(8).toString("hex")}.tmp`);
  try {
    await writePrivateFile(pointerPath, {
      schema_id: "pullwise-managed-profile-pointer/v1",
      generation: options.generation,
      manifest_digest: options.state.manifestDigest,
    });
    await rename(pointerPath, path.join(root, "managed-current.json"));
    await syncDirectory(root);
  } finally {
    await rm(pointerPath, { force: true });
  }
}
