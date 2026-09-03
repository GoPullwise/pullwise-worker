import assert from "node:assert/strict";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { cli } from "../../src/main.ts";


test("retired profile CLI cannot create host-local provider credentials", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-retired-profile-cli-"));
  try {
    await assert.rejects(
      cli([
        "profile", "add", "--id", "openai_team", "--label", "OpenAI team",
        "--provider", "openai", "--api-key", "upstream-secret",
      ]),
      /profiles are centrally managed through Pullwise Model Gateway/u,
    );
    assert.deepEqual(await readdir(root), []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
