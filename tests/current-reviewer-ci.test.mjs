import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative) => readFileSync(path.join(ROOT, relative), "utf8").replace(/\r\n?/gu, "\n");

const RETIRED = Object.freeze([
  ".codereview",
  "pullwise_worker",
  "contracts",
  "deploy",
  "docs",
  "runtime",
  "MANIFEST.in",
  "pyproject.toml",
  "requirements-audit.txt",
  "setup.py",
]);

test("the Worker has one Node.js and Pi package target", () => {
  const manifest = JSON.parse(read("package.json"));
  assert.equal(manifest.engines.node, ">=22.19.0");
  assert.equal(manifest.dependencies["@earendil-works/pi-coding-agent"], "0.84.4");
  assert.equal(manifest.bin["pullwise-worker"], "src/main.ts");
  assert.equal(manifest.scripts.start, "node src/main.ts");
});

test("the Python, Codex, compatibility, and shadow runtime trees are absent", () => {
  for (const relative of RETIRED) {
    assert.equal(existsSync(path.join(ROOT, relative)), false, relative);
  }
});

test("CI installs and verifies only the Node target", () => {
  const workflow = read(".github/workflows/ci.yml");
  const release = read(".github/workflows/release.yml");
  for (const required of [
    'node-version: "22.23.1"',
    "npm ci --ignore-scripts",
    "npm test",
    "npm run typecheck",
    "node scripts/check-current-reviewer-target.mjs --repo worker",
  ]) {
    assert.ok(workflow.includes(required), required);
  }
  for (const forbidden of ["setup-python", "python ", "pytest", "pip ", "Codex", "CODEX_HOME"]) {
    assert.equal(workflow.includes(forbidden), false, forbidden);
    assert.equal(release.includes(forbidden), false, `release:${forbidden}`);
  }
});

test("release publishes the Node tarball at the Server installer URL shape", () => {
  const release = read(".github/workflows/release.yml");
  for (const required of [
    "contents: write",
    "npm version",
    "npm pack --ignore-scripts",
    "gh release create",
    "gh release upload",
    "pullwise-worker-*.tgz",
  ]) {
    assert.ok(release.includes(required), required);
  }
  assert.equal(release.includes(".whl"), false);
});

test("production source has no Codex or Python compatibility path", () => {
  const production = [
    read("package.json"),
    read("src/main.ts"),
    read("src/runtime/pi-session.ts"),
    read("src/runtime/attempt-supervisor.ts"),
  ].join("\n");
  for (const forbidden of ["Codex", "CODEX_HOME", "openai_codex", "codex_sdk", "python"]) {
    assert.equal(production.toLowerCase().includes(forbidden.toLowerCase()), false, forbidden);
  }
  assert.equal(production.includes("@earendil-works/pi-coding-agent"), true);
});
