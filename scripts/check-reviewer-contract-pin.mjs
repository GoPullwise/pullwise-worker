#!/usr/bin/env node

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  lstatSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { isDeepStrictEqual } from "node:util";

const FILES = Object.freeze({
  "index.js": Object.freeze({
    size_bytes: 11227,
    sha256: "sha256:c3db5b6b4d10d9fb97c805c724585500d2352d293d3a8ed9b3964cca33809057",
  }),
  "package.json": Object.freeze({
    size_bytes: 506,
    sha256: "sha256:b7998c233542c66db10d394ad116e1c5d329f16ba3f123010c978db57ea01bd7",
  }),
  "schema.json": Object.freeze({
    size_bytes: 80741,
    sha256: "sha256:39dd603502669542b9e16b30d60522796794014307ae0335b2f892d551f0c6dd",
  }),
});
const FILE_NAMES = Object.freeze(Object.keys(FILES).sort());
const VENDOR_RELATIVE = "vendor/generated/reviewer-contract-npm";
const DEPENDENCY_SPEC = `file:${VENDOR_RELATIVE}`;
const PYTHON_TARGETS = Object.freeze([
  "pullwise_worker/_generated_reviewer_contract.py",
  "scripts/check_reviewer_contract_pin.py",
  "tests/reviewer/test_contract_pin.py",
]);

const EXPECTED_PIN = Object.freeze({
  schema_id: "pullwise-reviewer-contract-npm-pin/v1",
  contract_version: "pullwise-review/v1",
  canonicalization: "pullwise-canonical-json/v1",
  manifest_digest: "sha256:71428f4dc199e7cbdbe99b64cbdeff03686cda59eb08e84f22224822f5a8167e",
  source_card_id: "R1-PI-03",
  source_handoff_sha256: "sha256:6c478ec3934c300e1555fbbdf3ac840f9e7a2197593b1764056b0f99fabc3168",
  source_commit: "190111a81ef00f54fb9a514b821ad3d578e84914",
  source_tree: "a25c8751268e74a565c59148bfe326f115564f8e",
  source_path: "generated/reviewer-contract-npm",
  package_name: "pullwise-review-contract",
  package_version: "1.0.0",
  dependency_spec: DEPENDENCY_SPEC,
  consumer_path: VENDOR_RELATIVE,
  files: FILES,
});

const EXPECTED_VENDOR_MANIFEST = Object.freeze({
  contract: Object.freeze({
    canonicalization: "pullwise-canonical-json/v1",
    contract_version: "pullwise-review/v1",
    manifest_digest: "sha256:71428f4dc199e7cbdbe99b64cbdeff03686cda59eb08e84f22224822f5a8167e",
    schema_id: "pullwise-review-consumer-npm/v1",
  }),
  description: "Deterministic npm contract consumer for pullwise-review/v1 (generated; do not edit).",
  exports: "./index.js",
  files: Object.freeze(["index.js", "schema.json"]),
  license: "UNLICENSED",
  name: "pullwise-review-contract",
  private: true,
  type: "module",
  version: "1.0.0",
});

const PI_PACKAGE = "@earendil-works/pi-coding-agent";
const PI_VERSION = "0.84.4";

export class ContractPinError extends Error {}

function fail(message) {
  throw new ContractPinError(message);
}

function parseStrictJson(bytes, label) {
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch (error) {
    fail(`${label}: invalid UTF-8 (${error.message})`);
  }
  let index = 0;
  const whitespace = () => {
    while (/\s/u.test(text[index] ?? "")) index += 1;
  };
  const string = () => {
    const start = index;
    index += 1;
    while (index < text.length) {
      const code = text.charCodeAt(index);
      if (text[index] === '"') {
        index += 1;
        try {
          const value = JSON.parse(text.slice(start, index));
          if (value.normalize("NFC") !== value) fail(`${label}: non-NFC string`);
          return value;
        } catch (error) {
          if (error instanceof ContractPinError) throw error;
          fail(`${label}: malformed string`);
        }
      }
      if (text[index] === "\\") {
        index += 1;
        if (text[index] === "u") index += 4;
      } else if (code < 0x20) {
        fail(`${label}: control character in string`);
      }
      index += 1;
    }
    fail(`${label}: unterminated string`);
  };
  const value = () => {
    whitespace();
    if (text[index] === '"') return string();
    if (text[index] === "{") return object();
    if (text[index] === "[") return array();
    for (const [token, parsed] of [["true", true], ["false", false], ["null", null]]) {
      if (text.startsWith(token, index)) {
        index += token.length;
        return parsed;
      }
    }
    const match = text.slice(index).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/u);
    if (!match) fail(`${label}: malformed JSON at byte ${index}`);
    index += match[0].length;
    const parsed = Number(match[0]);
    if (!Number.isFinite(parsed)) fail(`${label}: non-finite number`);
    return parsed;
  };
  const object = () => {
    index += 1;
    const result = {};
    const keys = new Set();
    whitespace();
    if (text[index] === "}") {
      index += 1;
      return result;
    }
    while (true) {
      whitespace();
      if (text[index] !== '"') fail(`${label}: object key must be a string`);
      const key = string();
      if (keys.has(key)) fail(`${label}: duplicate key ${JSON.stringify(key)}`);
      keys.add(key);
      whitespace();
      if (text[index] !== ":") fail(`${label}: missing colon`);
      index += 1;
      result[key] = value();
      whitespace();
      if (text[index] === "}") {
        index += 1;
        return result;
      }
      if (text[index] !== ",") fail(`${label}: missing comma`);
      index += 1;
    }
  };
  const array = () => {
    index += 1;
    const result = [];
    whitespace();
    if (text[index] === "]") {
      index += 1;
      return result;
    }
    while (true) {
      result.push(value());
      whitespace();
      if (text[index] === "]") {
        index += 1;
        return result;
      }
      if (text[index] !== ",") fail(`${label}: missing comma`);
      index += 1;
    }
  };
  const parsed = value();
  whitespace();
  if (index !== text.length) fail(`${label}: trailing JSON content`);
  return parsed;
}

function safeDirectory(path, label) {
  let status;
  try {
    status = lstatSync(path);
  } catch (error) {
    fail(`${label}: missing (${error.code ?? error.message})`);
  }
  if (status.isSymbolicLink() || !status.isDirectory()) fail(`${label}: unsafe directory`);
}

function readRegular(path, label) {
  let before;
  try {
    before = lstatSync(path);
  } catch (error) {
    fail(`${label}: missing (${error.code ?? error.message})`);
  }
  if (before.isSymbolicLink() || !before.isFile()) fail(`${label}: unsafe file`);
  const bytes = readFileSync(path);
  const after = statSync(path);
  if (before.dev !== after.dev || before.ino !== after.ino || before.size !== after.size) {
    fail(`${label}: changed while reading`);
  }
  return bytes;
}

function exact(actual, expected, label) {
  if (!isDeepStrictEqual(actual, expected)) fail(`${label}: closed object mismatch`);
}

function verifyRootManifest(manifest) {
  const valid =
    manifest?.name === "pullwise-worker" &&
    manifest?.version === "0.10.24" &&
    manifest?.private === true &&
    manifest?.type === "module" &&
    manifest?.engines?.node === ">=22.19.0" &&
    manifest?.packageManager === "npm@10.9.8" &&
    manifest?.dependencies?.["pullwise-review-contract"] === DEPENDENCY_SPEC &&
    manifest?.dependencies?.[PI_PACKAGE] === PI_VERSION;
  if (!valid) fail("package.json: closed object mismatch");
}

function verifyLock(lock) {
  const root = lock?.packages?.[""];
  const local = lock?.packages?.["node_modules/pullwise-review-contract"];
  const vendor = lock?.packages?.[VENDOR_RELATIVE];
  const pi = lock?.packages?.[`node_modules/${PI_PACKAGE}`];
  const valid =
    lock?.name === "pullwise-worker" &&
    lock?.version === "0.10.24" &&
    lock?.lockfileVersion === 3 &&
    lock?.requires === true &&
    root?.dependencies?.["pullwise-review-contract"] === DEPENDENCY_SPEC &&
    root?.dependencies?.[PI_PACKAGE] === PI_VERSION &&
    local?.resolved === VENDOR_RELATIVE &&
    local?.link === true &&
    vendor?.name === "pullwise-review-contract" &&
    vendor?.version === "1.0.0" &&
    pi?.version === PI_VERSION;
  if (!valid) fail("package-lock.json: closed object mismatch");
}

function digest(bytes) {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

function verifyPackageFiles(root, label) {
  safeDirectory(root, label);
  const entries = readdirSync(root).sort();
  exact(entries, FILE_NAMES, `${label} entries`);
  const result = {};
  for (const filename of FILE_NAMES) {
    const bytes = readRegular(join(root, filename), `${label}/${filename}`);
    const expected = FILES[filename];
    if (bytes.length !== expected.size_bytes || digest(bytes) !== expected.sha256) {
      fail(`${label}/${filename}: byte identity mismatch`);
    }
    result[filename] = bytes;
  }
  exact(
    parseStrictJson(result["package.json"], `${label}/package.json`),
    EXPECTED_VENDOR_MANIFEST,
    `${label}/package.json`,
  );
  parseStrictJson(result["schema.json"], `${label}/schema.json`);
  return result;
}

function verifyInstalled(root, vendorRoot, vendorFiles) {
  const installed = join(root, "node_modules", "pullwise-review-contract");
  let checkedRoot = installed;
  let status;
  try {
    status = lstatSync(installed);
  } catch (error) {
    fail(`installed package: missing (${error.code ?? error.message})`);
  }
  if (status.isSymbolicLink()) {
    if (realpathSync(installed) !== realpathSync(vendorRoot)) {
      fail("installed package: local link target mismatch");
    }
    checkedRoot = realpathSync(installed);
  } else if (!status.isDirectory()) {
    fail("installed package: unsafe directory");
  }
  const installedFiles = verifyPackageFiles(checkedRoot, "installed package");
  for (const filename of FILE_NAMES) {
    if (!installedFiles[filename].equals(vendorFiles[filename])) {
      fail(`installed package/${filename}: differs from vendor`);
    }
  }
}

export function checkReviewerContractPin(repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..")) {
  const root = realpathSync(repoRoot);
  safeDirectory(root, "repository root");
  const pin = parseStrictJson(
    readRegular(join(root, "reviewer-contract-pin.json"), "reviewer-contract-pin.json"),
    "reviewer-contract-pin.json",
  );
  exact(pin, EXPECTED_PIN, "reviewer-contract-pin.json");
  verifyRootManifest(
    parseStrictJson(readRegular(join(root, "package.json"), "package.json"), "package.json"),
  );
  verifyLock(
    parseStrictJson(readRegular(join(root, "package-lock.json"), "package-lock.json"), "package-lock.json"),
  );
  const vendorRoot = join(root, ...VENDOR_RELATIVE.split("/"));
  const vendorFiles = verifyPackageFiles(vendorRoot, VENDOR_RELATIVE);
  verifyInstalled(root, vendorRoot, vendorFiles);
  for (const relative of PYTHON_TARGETS) {
    try {
      lstatSync(join(root, ...relative.split("/")));
      fail(`${relative}: retired Python target remains`);
    } catch (error) {
      if (error instanceof ContractPinError) throw error;
      if (error.code !== "ENOENT") fail(`${relative}: cannot prove absence`);
    }
  }
  return Object.freeze({ files: FILE_NAMES.length, manifest_digest: EXPECTED_PIN.manifest_digest });
}

function parseArgs(argv) {
  if (argv.length === 0) return undefined;
  if (argv.length === 2 && argv[0] === "--repo-root") return argv[1];
  fail("usage: check-reviewer-contract-pin.mjs [--repo-root PATH]");
}

function main() {
  try {
    checkReviewerContractPin(parseArgs(process.argv.slice(2)));
    console.log("ok: reviewer contract pin is pristine");
    return 0;
  } catch (error) {
    if (error instanceof ContractPinError || error instanceof assert.AssertionError) {
      console.error(`error: ${error.message}`);
      return 1;
    }
    console.error(`error: ${error.message}`);
    return 2;
  }
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  process.exitCode = main();
}
