import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { buildRuntimeCatalog, loadProfiles } from "../../src/runtime/profiles.ts";


const DIGEST = "a".repeat(64);
const GENERATION = `${DIGEST}.gtj_profiles`;

async function installManagedMetadata(root: string): Promise<void> {
  const generationRoot = join(root, "generations", GENERATION);
  await mkdir(join(generationRoot, "profiles", "gateway-reviewer-production"), { recursive: true });
  await writeFile(join(root, "managed-current.json"), JSON.stringify({
    schema_id: "pullwise-managed-profile-pointer/v1",
    generation: GENERATION,
    manifest_digest: DIGEST,
  }));
  await writeFile(join(generationRoot, "profiles.json"), JSON.stringify({
    schema_id: "pullwise-pi-profiles/v1",
    profiles: [{
      credential_id: "gateway-reviewer-production",
      label: "Reviewer production",
      provider: "pullwise-gateway",
      auth_type: "api_key",
      agent_dir: "profiles/gateway-reviewer-production",
    }],
  }));
}

test("managed profile metadata builds a de-secreted catalog", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-pi-profiles-"));
  try {
    await installManagedMetadata(root);
    const profiles = await loadProfiles(root);
    assert.equal(profiles.profiles.length, 1);
    const catalog = await buildRuntimeCatalog(profiles, async () => [
      { id: "gpt-reviewer", name: "GPT Reviewer" },
    ]);
    assert.deepEqual(catalog, {
      schema_id: "pullwise-pi-runtime-catalog/v1",
      credentials: [{
        credential_id: "gateway-reviewer-production",
        label: "Reviewer production",
        provider: "pullwise-gateway",
        auth_type: "api_key",
        models: [{ id: "gpt-reviewer", name: "GPT Reviewer" }],
      }],
    });
    assert.doesNotMatch(JSON.stringify(catalog), /"(?:apiKey|api_key|secret|token)"\s*:/iu);
    const stored = await readFile(join(root, "generations", GENERATION, "profiles.json"), "utf8");
    assert.doesNotMatch(stored, /"(?:apiKey|api_key|secret|token)"\s*:/iu);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("unmanaged legacy profile roots fail closed", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-pi-profiles-invalid-"));
  try {
    await writeFile(join(root, "profiles.json"), JSON.stringify({
      schema_id: "pullwise-pi-profiles/v1",
      profiles: [],
    }));
    await assert.rejects(loadProfiles(root), /managed profile pointer is required/u);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
