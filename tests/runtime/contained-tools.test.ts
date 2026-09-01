import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { createContainedReviewTools } from "../../src/runtime/contained-tools.ts";

test("contained read accepts workspace files and rejects absolute escape", async () => {
  const root = await mkdtemp(join(tmpdir(), "pullwise-contained-root-"));
  const outside = await mkdtemp(join(tmpdir(), "pullwise-contained-outside-"));
  try {
    const insidePath = join(root, "inside.txt");
    const outsidePath = join(outside, "secret.txt");
    await writeFile(insidePath, "inside\n", "utf8");
    await writeFile(outsidePath, "secret\n", "utf8");
    const tools = await createContainedReviewTools(root);
    const read = tools.find((tool) => tool.name === "repo_read");
    assert.ok(read);

    const result = await read.execute("call", { path: insidePath }, undefined, undefined, {} as never);
    assert.match(result.content[0]?.type === "text" ? result.content[0].text : "", /inside/u);
    await assert.rejects(
      read.execute("call", { path: outsidePath }, undefined, undefined, {} as never),
      /outside the attempt workspace/u,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
    await rm(outside, { recursive: true, force: true });
  }
});
