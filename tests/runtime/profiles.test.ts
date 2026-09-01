import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  addProfile,
  buildRuntimeCatalog,
  loadProfiles,
} from "../../src/runtime/profiles.ts";

test("profiles keep credential metadata separate and build a de-secreted catalog", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-pi-profiles-"));
  try {
    const anthropic = await addProfile(root, {
      credentialId: "anthropic_primary",
      label: "Anthropic primary",
      provider: "anthropic",
    });
    const openai = await addProfile(root, {
      credentialId: "openai_team",
      label: "OpenAI team",
      provider: "openai",
      authType: "oauth",
    });
    const profiles = await loadProfiles(root);
    assert.equal(profiles.profiles.length, 2);
    assert.match(anthropic.agentDir, /anthropic_primary/u);
    assert.match(openai.agentDir, /openai_team/u);

    const catalog = await buildRuntimeCatalog(profiles, async (profile) => [
      { id: `${profile.provider}-model`, name: `${profile.label} model` },
    ]);
    assert.deepEqual(catalog, {
      schema_id: "pullwise-pi-runtime-catalog/v1",
      credentials: [
        {
          credential_id: "anthropic_primary",
          label: "Anthropic primary",
          provider: "anthropic",
          auth_type: "api_key",
          models: [{ id: "anthropic-model", name: "Anthropic primary model" }],
        },
        {
          credential_id: "openai_team",
          label: "OpenAI team",
          provider: "openai",
          auth_type: "oauth",
          models: [{ id: "openai-model", name: "OpenAI team model" }],
        },
      ],
    });
    assert.doesNotMatch(JSON.stringify(catalog), /"(?:apiKey|api_key|secret|token)"\s*:/iu);

    const stored = await readFile(join(root, "profiles.json"), "utf8");
    assert.doesNotMatch(stored, /"(?:apiKey|api_key|secret|token)"\s*:/iu);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("profile config rejects secret-bearing or unknown fields", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-pi-profiles-invalid-"));
  try {
    await writeFile(
      join(root, "profiles.json"),
      JSON.stringify({
        schema_id: "pullwise-pi-profiles/v1",
        profiles: [{
          credential_id: "unsafe",
          label: "Unsafe",
          provider: "openai",
          agent_dir: "profiles/unsafe",
          api_key: "must-not-be-stored",
        }],
      }),
      "utf8",
    );
    await assert.rejects(loadProfiles(root), /closed metadata object/u);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
