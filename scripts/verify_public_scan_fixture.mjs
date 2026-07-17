#!/usr/bin/env node

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const WORKER_ROOT = resolve(SCRIPT_DIR, "..");
const WORKSPACE_ROOT = resolve(WORKER_ROOT, "..");
const FIXTURE_PATH = resolve(
  WORKER_ROOT,
  "contracts",
  "agent-first",
  "fixtures",
  "public-scan-v1.json"
);
const WEB_ROOT = resolve(WORKSPACE_ROOT, "pullwise-web");
const VITE_MODULE_URL = pathToFileURL(
  resolve(WEB_ROOT, "node_modules", "vite", "dist", "node", "index.js")
).href;
const SCHEMA_ID = "pullwise-public-scan-fixture-pack/v1";
const CASE_IDS = [
  "running_with_estimate",
  "partial_completed",
  "completed_with_debug_bundle_and_duration",
];

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function assertSubset(actual, expected, location) {
  if (Array.isArray(expected)) {
    assert.ok(Array.isArray(actual), `${location}: expected an array`);
    assert.equal(actual.length, expected.length, `${location}: array length`);
    expected.forEach((item, index) => {
      assertSubset(actual[index], item, `${location}[${index}]`);
    });
    return;
  }
  if (isRecord(expected)) {
    assert.ok(isRecord(actual), `${location}: expected an object`);
    for (const [key, value] of Object.entries(expected)) {
      assert.ok(
        Object.prototype.hasOwnProperty.call(actual, key),
        `${location}: missing key ${key}`
      );
      assertSubset(actual[key], value, `${location}.${key}`);
    }
    return;
  }
  assert.deepEqual(actual, expected, location);
}

function validatePack(pack) {
  assert.ok(isRecord(pack), "fixture pack must be an object");
  assert.equal(pack.schema_id, SCHEMA_ID, "fixture schema_id");
  assert.ok(Array.isArray(pack.cases), "fixture cases must be an array");
  assert.deepEqual(
    pack.cases.map((item) => item?.id),
    CASE_IDS,
    "fixture case ids"
  );
  for (const fixtureCase of pack.cases) {
    assert.ok(isRecord(fixtureCase.input), `${fixtureCase.id}: input must be an object`);
    assert.ok(isRecord(fixtureCase.expected), `${fixtureCase.id}: expected must be an object`);
    assert.ok(
      isRecord(fixtureCase.expected.normalized_subset),
      `${fixtureCase.id}: normalized_subset must be an object`
    );
    assert.equal(
      typeof fixtureCase.expected.is_terminal,
      "boolean",
      `${fixtureCase.id}: is_terminal must be boolean`
    );
    assert.equal(
      typeof fixtureCase.expected.can_download_audit_bundle,
      "boolean",
      `${fixtureCase.id}: can_download_audit_bundle must be boolean`
    );
  }
}

async function loadWebModule() {
  const { createServer } = await import(VITE_MODULE_URL);
  const scratchRoot =
    process.env.TEMP || process.env.TMP || process.env.TMPDIR || WORKER_ROOT;
  const server = await createServer({
    root: WEB_ROOT,
    appType: "custom",
    cacheDir: resolve(scratchRoot, "pullwise-contract-vite"),
    logLevel: "silent",
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false },
  });
  try {
    return await server.ssrLoadModule("/src/lib/pullwise-data.js");
  } finally {
    await server.close();
  }
}

async function main() {
  const pack = JSON.parse(await readFile(FIXTURE_PATH, "utf8"));
  validatePack(pack);

  const web = await loadWebModule();
  for (const exportName of [
    "normalizeScan",
    "isTerminalScan",
    "scanCanDownloadAuditBundle",
  ]) {
    assert.equal(typeof web[exportName], "function", `missing Web export ${exportName}`);
  }

  for (const fixtureCase of pack.cases) {
    const normalized = web.normalizeScan(fixtureCase.input);
    assertSubset(
      normalized,
      fixtureCase.expected.normalized_subset,
      `${fixtureCase.id}.normalized_subset`
    );
    assert.equal(
      web.isTerminalScan(normalized),
      fixtureCase.expected.is_terminal,
      `${fixtureCase.id}.is_terminal`
    );
    assert.equal(
      web.scanCanDownloadAuditBundle(normalized),
      fixtureCase.expected.can_download_audit_bundle,
      `${fixtureCase.id}.can_download_audit_bundle`
    );
  }

  console.log(
    JSON.stringify({
      success: true,
      numTotalTests: pack.cases.length,
      numPassedTests: pack.cases.length,
      numFailedTests: 0,
      numPendingTests: 0,
      numTodoTests: 0,
      numFailedTestSuites: 0,
    })
  );
}

main().catch((error) => {
  const detail = error instanceof Error ? error.stack || error.message : String(error);
  console.error(`public scan fixture verification failed\n${detail}`);
  process.exitCode = 1;
});
