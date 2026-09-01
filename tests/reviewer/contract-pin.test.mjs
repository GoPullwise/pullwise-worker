import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const pinPath = resolve(repoRoot, "reviewer-contract-pin.json");
const vendorRoot = resolve(repoRoot, "vendor/generated/reviewer-contract-npm");
const checkerPath = resolve(repoRoot, "scripts/check-reviewer-contract-pin.mjs");
const retiredPythonTargets = Object.freeze([
  "pullwise_worker/_generated_reviewer_contract.py",
  "scripts/check_reviewer_contract_pin.py",
  "tests/reviewer/test_contract_pin.py",
]);
const expectedFiles = Object.freeze({
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

function parse(relativePath) {
  return JSON.parse(readFileSync(resolve(repoRoot, relativePath), "utf8"));
}

function sha256(bytes) {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

function run(command, args, cwd = repoRoot) {
  const executable = command === "npm" ? process.execPath : command;
  const commandArgs = command === "npm"
    ? [resolve(dirname(process.execPath), "node_modules/npm/bin/npm-cli.js"), ...args]
    : args;
  return spawnSync(executable, commandArgs, {
    cwd,
    encoding: "utf8",
    env: process.env,
    shell: false,
  });
}

function copyFixture(destination, installed = "link") {
  mkdirSync(resolve(destination, "scripts"), { recursive: true });
  mkdirSync(resolve(destination, "vendor/generated"), { recursive: true });
  for (const filename of ["package.json", "package-lock.json", "reviewer-contract-pin.json"]) {
    cpSync(resolve(repoRoot, filename), resolve(destination, filename));
  }
  cpSync(checkerPath, resolve(destination, "scripts/check-reviewer-contract-pin.mjs"));
  cpSync(vendorRoot, resolve(destination, "vendor/generated/reviewer-contract-npm"), {
    recursive: true,
  });
  if (installed === "none") return;
  mkdirSync(resolve(destination, "node_modules"), { recursive: true });
  const installedRoot = resolve(destination, "node_modules/pullwise-review-contract");
  if (installed === "copy") {
    cpSync(resolve(destination, "vendor/generated/reviewer-contract-npm"), installedRoot, {
      recursive: true,
    });
  } else {
    symlinkSync("../vendor/generated/reviewer-contract-npm", installedRoot, "dir");
  }
}

function withFixture(callback, installed = "link") {
  const fixture = mkdtempSync(join(tmpdir(), "reviewer-contract-pin-"));
  try {
    copyFixture(fixture, installed);
    return callback(fixture);
  } finally {
    rmSync(fixture, { force: true, recursive: true });
  }
}

function checkFixture(fixture) {
  return run(
    process.execPath,
    [resolve(fixture, "scripts/check-reviewer-contract-pin.mjs"), "--repo-root", fixture],
    fixture,
  );
}

test("the reviewer pin names the npm consumer", () => {
  const pin = JSON.parse(readFileSync(pinPath, "utf8"));

  assert.equal(pin.schema_id, "pullwise-reviewer-contract-npm-pin/v1");
  assert.equal(pin.consumer_path, "vendor/generated/reviewer-contract-npm");
  assert.equal(pin.source_card_id, "R1-PI-03");
  assert.equal(
    pin.source_handoff_sha256,
    "sha256:6c478ec3934c300e1555fbbdf3ac840f9e7a2197593b1764056b0f99fabc3168",
  );
  assert.deepEqual(pin.files, expectedFiles);
});

test("the Worker root declares its npm contract dependency", () => {
  assert.ok(existsSync(resolve(repoRoot, "package.json")), "package.json is absent");
  const manifest = parse("package.json");
  assert.equal(
    manifest.dependencies["pullwise-review-contract"],
    "file:vendor/generated/reviewer-contract-npm",
  );
  assert.equal(manifest.packageManager, "npm@10.9.8");
  assert.equal(manifest.dependencies["@earendil-works/pi-coding-agent"], "0.84.4");
  assert.equal(manifest.devDependencies.typescript, "7.0.2");
});

test("the vendored npm contract contains the closed three-file surface", () => {
  assert.deepEqual(readdirSync(vendorRoot).sort(), Object.keys(expectedFiles).sort());
  for (const [filename, expected] of Object.entries(expectedFiles)) {
    const bytes = readFileSync(resolve(vendorRoot, filename));
    assert.equal(bytes.length, expected.size_bytes, filename);
    assert.equal(sha256(bytes), expected.sha256, filename);
  }
});

test("the generated package exposes only the frozen named ESM surface", async () => {
  const contract = await import(`${pathToFileURL(resolve(vendorRoot, "index.js")).href}?live`);
  assert.deepEqual(Object.keys(contract).sort(), [
    "CANONICALIZATION",
    "CONTRACT_VERSION",
    "FILES",
    "HTTP_STATUS_BY_ERROR_CODE",
    "MANIFEST_DIGEST",
    "REGISTRIES",
    "SCHEMA",
    "classifyErrorCode",
    "validateDefinition",
    "validateDocument",
  ]);
  assert.equal(contract.CONTRACT_VERSION, "pullwise-review/v1");
  assert.equal(contract.CANONICALIZATION, "pullwise-canonical-json/v1");
  assert.deepEqual(contract.SCHEMA, parse("vendor/generated/reviewer-contract-npm/schema.json"));
  assert.deepEqual(contract.validateDefinition("Digest", `sha256:${"0".repeat(64)}`), []);
  assert.notDeepEqual(contract.validateDefinition("Digest", "invalid"), []);
  assert.notDeepEqual(contract.validateDocument({}), []);
});

test("the lock graph binds the local contract and exact Pi runtime", () => {
  const lock = parse("package-lock.json");
  assert.equal(lock.lockfileVersion, 3);
  assert.equal(lock.packages[""].dependencies["@earendil-works/pi-coding-agent"], "0.84.4");
  assert.deepEqual(lock.packages["node_modules/pullwise-review-contract"], {
    resolved: "vendor/generated/reviewer-contract-npm",
    link: true,
  });
  assert.equal(lock.packages["node_modules/@earendil-works/pi-coding-agent"].version, "0.84.4");
});

test("the contract-specific Python consumer target is absent", () => {
  for (const relativePath of retiredPythonTargets) {
    assert.equal(existsSync(resolve(repoRoot, relativePath)), false, relativePath);
  }
});

test("the checker passes in an isolated Worker-only filesystem", () => {
  withFixture((fixture) => {
    const result = checkFixture(fixture);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /ok: reviewer contract pin is pristine/u);
  });
});

for (const [name, installed, mutate, expected] of [
  [
    "missing vendored file",
    "link",
    (root) => rmSync(resolve(root, "vendor/generated/reviewer-contract-npm/index.js")),
    /entries|missing/u,
  ],
  [
    "tampered vendored file",
    "link",
    (root) => writeFileSync(resolve(root, "vendor/generated/reviewer-contract-npm/index.js"), "tampered"),
    /byte identity mismatch/u,
  ],
  [
    "unknown pin field",
    "link",
    (root) => {
      const pin = JSON.parse(readFileSync(resolve(root, "reviewer-contract-pin.json"), "utf8"));
      pin.unknown = true;
      writeFileSync(resolve(root, "reviewer-contract-pin.json"), JSON.stringify(pin));
    },
    /closed object mismatch/u,
  ],
  [
    "duplicate pin key",
    "link",
    (root) => {
      const path = resolve(root, "reviewer-contract-pin.json");
      writeFileSync(path, readFileSync(path, "utf8").replace("{", '{"schema_id":"duplicate",'));
    },
    /duplicate key/u,
  ],
  [
    "malformed UTF-8 pin",
    "link",
    (root) => writeFileSync(resolve(root, "reviewer-contract-pin.json"), Buffer.from([0xff])),
    /invalid UTF-8/u,
  ],
  [
    "root manifest drift",
    "link",
    (root) => {
      const path = resolve(root, "package.json");
      writeFileSync(path, readFileSync(path, "utf8").replace("file:vendor", "file:other"));
    },
    /closed object mismatch/u,
  ],
  [
    "lock graph drift",
    "link",
    (root) => {
      const path = resolve(root, "package-lock.json");
      writeFileSync(path, readFileSync(path, "utf8").replace('"requires": true', '"requires": false'));
    },
    /closed object mismatch/u,
  ],
  [
    "extra generated file",
    "link",
    (root) => writeFileSync(resolve(root, "vendor/generated/reviewer-contract-npm/extra.txt"), "extra"),
    /entries/u,
  ],
  [
    "vendored symlink",
    "link",
    (root) => {
      const target = resolve(root, "vendor/generated/reviewer-contract-npm/schema.json");
      rmSync(target);
      symlinkSync("package.json", target);
    },
    /unsafe file/u,
  ],
  [
    "installed byte drift",
    "copy",
    (root) => writeFileSync(resolve(root, "node_modules/pullwise-review-contract/index.js"), "tampered"),
    /byte identity mismatch/u,
  ],
  [
    "retired Python target",
    "link",
    (root) => {
      mkdirSync(resolve(root, "pullwise_worker"));
      writeFileSync(resolve(root, "pullwise_worker/_generated_reviewer_contract.py"), "legacy");
    },
    /retired Python target remains/u,
  ],
]) {
  test(`the checker rejects ${name}`, () => {
    withFixture((fixture) => {
      mutate(fixture);
      const result = checkFixture(fixture);
      assert.equal(result.status, 1, `${result.stdout}\n${result.stderr}`);
      assert.match(result.stderr, expected);
    }, installed);
  });
}

test("a clean offline npm ci installs bytes identical to the vendor", () => {
  withFixture((fixture) => {
    const install = run(
      "npm",
      ["ci", "--ignore-scripts", "--offline", "--no-audit", "--no-fund"],
      fixture,
    );
    assert.equal(install.status, 0, `${install.stdout}\n${install.stderr}`);
    const checked = checkFixture(fixture);
    assert.equal(checked.status, 0, checked.stderr);
    for (const filename of Object.keys(expectedFiles)) {
      assert.deepEqual(
        readFileSync(resolve(fixture, "node_modules/pullwise-review-contract", filename)),
        readFileSync(resolve(fixture, "vendor/generated/reviewer-contract-npm", filename)),
      );
    }
  }, "none");
});

test("npm package dry-run exposes exactly the three generated files", () => {
  const packed = run(
    "npm",
    [
      "pack",
      "--dry-run",
      "--json",
      "--ignore-scripts",
      "./vendor/generated/reviewer-contract-npm",
    ],
  );
  assert.equal(packed.status, 0, `${packed.stdout}\n${packed.stderr}`);
  const payload = JSON.parse(packed.stdout);
  assert.equal(payload.length, 1);
  assert.deepEqual(
    payload[0].files.map((entry) => entry.path).sort(),
    ["index.js", "package.json", "schema.json"],
  );
  assert.equal(
    relative(repoRoot, vendorRoot).replaceAll("\\", "/"),
    "vendor/generated/reviewer-contract-npm",
  );
});
