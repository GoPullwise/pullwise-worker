import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { runProfileCommand } from "../../src/profile-cli.ts";

test("profile add prints a per-profile Pi auth command without accepting secrets", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-profile-cli-"));
  try {
    const lines: string[] = [];
    const code = await runProfileCommand(
      ["add", "--id", "openai_team", "--label", "OpenAI team", "--provider", "openai", "--auth-type", "oauth"],
      { profileRoot: root, write: (line) => lines.push(line) },
    );
    assert.equal(code, 0);
    const payload = JSON.parse(lines.join(""));
    assert.equal(payload.profile.credentialId, "openai_team");
    assert.equal(payload.profile.authType, "oauth");
    assert.match(payload.authCommand, /PI_CODING_AGENT_DIR=/u);
    assert.match(payload.authCommand, /pi auth login --provider 'openai'/u);
    assert.doesNotMatch(lines.join(""), /api[_-]?key.*=/iu);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("profile add rejects secret-bearing flags", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-profile-cli-secret-"));
  try {
    await assert.rejects(
      runProfileCommand(
        ["add", "--id", "x", "--label", "X", "--provider", "openai", "--api-key", "secret"],
        { profileRoot: root, write: () => {} },
      ),
      /unknown option/u,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
