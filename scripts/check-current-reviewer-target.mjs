#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

export const REPORT_SCHEMA_ID = "pullwise-current-reviewer-target-report/v1";
export const CURRENT_START = "<!-- PULLWISE_REVIEWER_CURRENT_AUTHORITY_START -->";
export const CURRENT_END = "<!-- PULLWISE_REVIEWER_CURRENT_AUTHORITY_END -->";
export const TARGET_START = "<!-- PULLWISE_REVIEWER_TARGET_START -->";
export const TARGET_END = "<!-- PULLWISE_REVIEWER_TARGET_END -->";
export const CURRENT_BLOCK_SHA256 =
  "24435fb38cf3b04c77243fb20df00fc3ceb928ebe560e4b8682c8e3c8f36deeb";
export const TARGET_BLOCK_SHA256 =
  "c61800c199d637568022d730f0758c7c523f44009a1aed66be20ea5034ef5eaa";
export const REPOSITORIES = Object.freeze({
  admin: "pullwise-admin",
  server: "pullwise-server",
  web: "pullwise-web",
  worker: "pullwise-worker",
});

export const TARGET_BLOCK = `${TARGET_START}
## Pullwise Reviewer Target — Node.js + Pi Coding Agent

For all new Pullwise Reviewer implementation, the sole Worker target is
Node.js/TypeScript on Node \`>=22.19.0\`, embedding
\`@earendil-works/pi-coding-agent\`. One active attempt owns one Pi
\`AgentSession\`.

This is a clean break. Do not add or preserve a Codex SDK or CLI, \`CODEX_HOME\`,
a Python Worker runtime, compatibility or shadow adapters, dual runtimes,
provider routing, or automatic provider/model fallback. Pi is not a sandbox:
the Worker supervisor must enforce operating-system containment, process-tree
ownership, cancellation, cleanup, and late-publication fencing.

Do not query or poll subscription/account quota windows, percentages, reset
times, low/exhausted readiness, or refresh-window commands. Preserve immutable
per-attempt input/output/cache-token usage, cost when reported, timing,
provider/model identity, and provider-error facts. Product account/repository
scan quotas are separate business controls and remain in force.

Any later Reviewer-specific Python, Codex, quota-window, runtime, phase, or
generated-consumer rule in this file is historical cleanup evidence only and
must not govern target implementation.
${TARGET_END}`;

function normalize(text) {
  return text.replace(/\r\n?/g, "\n");
}

function occurrences(text, needle) {
  let count = 0;
  let offset = 0;
  while ((offset = text.indexOf(needle, offset)) >= 0) {
    count += 1;
    offset += needle.length;
  }
  return count;
}

export function validateText(text) {
  const normalized = normalize(text);
  const errors = [];
  if (!normalized.startsWith(CURRENT_START + "\n")) {
    errors.push("missing_current_authority_block");
  }
  const currentEnd = normalized.indexOf(CURRENT_END);
  if (currentEnd < 0) {
    errors.push("unterminated_current_authority_block");
    return errors;
  }
  const currentStop = currentEnd + CURRENT_END.length;
  if (normalized[currentStop] !== "\n") {
    errors.push("current_authority_block_missing_trailing_lf");
  } else {
    const currentBlock = normalized.slice(0, currentStop + 1);
    const currentSha256 = crypto.createHash("sha256").update(currentBlock, "utf8").digest("hex");
    if (currentSha256 !== CURRENT_BLOCK_SHA256) {
      errors.push("current_authority_block_mismatch");
    }
  }
  if (
    occurrences(normalized, TARGET_START) !== 1 ||
    occurrences(normalized, TARGET_END) !== 1
  ) {
    errors.push("target_block_count_mismatch");
    return errors;
  }
  const targetStart = normalized.indexOf(TARGET_START);
  const targetEnd = normalized.indexOf(TARGET_END, targetStart);
  const targetStop = targetEnd + TARGET_END.length;
  const actual = normalized.slice(targetStart, targetStop);
  if (actual !== TARGET_BLOCK) {
    errors.push("target_block_mismatch");
  }
  if (normalized[targetStop] !== "\n") {
    errors.push("target_block_missing_trailing_lf");
  } else {
    const targetBlock = normalized.slice(targetStart, targetStop + 1);
    const targetSha256 = crypto.createHash("sha256").update(targetBlock, "utf8").digest("hex");
    if (targetSha256 !== TARGET_BLOCK_SHA256) {
      errors.push("target_block_mismatch");
    }
  }
  const expectedOffset = currentStop + 1;
  if (targetStart !== expectedOffset) {
    errors.push("target_block_not_immediately_after_authority");
  }
  return [...new Set(errors)].sort();
}

function isLink(metadata) {
  const reparsePoint = 0x400;
  const fileAttributes = metadata.fileAttributes ?? metadata.st_file_attributes ?? 0;
  return metadata.isSymbolicLink() ||
    Boolean((metadata.mode & 0o170000) === 0o120000) ||
    Boolean(fileAttributes & reparsePoint);
}

function samePath(left, right) {
  return path.relative(left, right) === "" && path.relative(right, left) === "";
}

function resolveWorkspaceRoot(workspaceRoot) {
  const lexicalRoot = path.resolve(workspaceRoot);
  let metadata;
  let resolvedRoot;
  try {
    metadata = fs.lstatSync(lexicalRoot);
    resolvedRoot = fs.realpathSync.native(lexicalRoot);
  } catch {
    throw new Error("workspace_root_unreadable");
  }
  if (!metadata.isDirectory() || isLink(metadata) || !samePath(lexicalRoot, resolvedRoot)) {
    throw new Error("workspace_root_not_safe");
  }
  return resolvedRoot;
}

function validateRepositoryAt(workspaceRoot, repository, directory) {
  const repositoryRoot = path.join(workspaceRoot, directory);
  const agentsPath = path.join(repositoryRoot, "AGENTS.md");
  try {
    const rootMetadata = fs.lstatSync(repositoryRoot);
    if (!rootMetadata.isDirectory() || isLink(rootMetadata)) {
      throw new Error("repository_path_not_safe");
    }
    const resolvedRepositoryRoot = fs.realpathSync.native(repositoryRoot);
    if (!samePath(path.dirname(resolvedRepositoryRoot), workspaceRoot)) {
      throw new Error("repository_path_outside_workspace");
    }
    const metadata = fs.lstatSync(agentsPath);
    if (!metadata.isFile() || isLink(metadata)) {
      throw new Error("agents_file_not_regular");
    }
    const resolvedAgentsPath = fs.realpathSync.native(agentsPath);
    if (!samePath(path.dirname(resolvedAgentsPath), resolvedRepositoryRoot)) {
      throw new Error("agents_file_outside_repository");
    }
    const raw = fs.readFileSync(resolvedAgentsPath);
    const text = new TextDecoder("utf-8", { fatal: true }).decode(raw);
    const errors = validateText(text);
    return {
      repository,
      path: `${directory}/AGENTS.md`,
      status: errors.length ? "FAIL" : "PASS",
      errors,
    };
  } catch (error) {
    const known = new Set([
      "repository_path_not_safe",
      "repository_path_outside_workspace",
      "agents_file_not_regular",
      "agents_file_outside_repository",
    ]);
    let code = known.has(error.message) ? error.message : "agents_file_unreadable";
    if (error instanceof TypeError && String(error.message).includes("encoded data")) {
      code = "agents_file_not_utf8";
    }
    return {
      repository,
      path: `${directory}/AGENTS.md`,
      status: "INDETERMINATE",
      errors: [code],
    };
  }
}

export function validateRepository(workspaceRoot, repository) {
  const directory = REPOSITORIES[repository];
  if (!directory) {
    return {
      schema_id: REPORT_SCHEMA_ID,
      status: "INDETERMINATE",
      repositories: [],
      errors: ["unknown_repository"],
    };
  }
  let root;
  try {
    root = resolveWorkspaceRoot(workspaceRoot);
  } catch (error) {
    return {
      schema_id: REPORT_SCHEMA_ID,
      status: "INDETERMINATE",
      repositories: [],
      errors: [error.message],
    };
  }
  const item = validateRepositoryAt(root, repository, directory);
  return {
    schema_id: REPORT_SCHEMA_ID,
    status: item.status,
    repositories: [item],
    errors: [],
  };
}

export function validateWorkspace(workspaceRoot) {
  let root;
  try {
    root = resolveWorkspaceRoot(workspaceRoot);
  } catch (error) {
    return {
      schema_id: REPORT_SCHEMA_ID,
      status: "INDETERMINATE",
      repositories: [],
      errors: [error.message],
    };
  }
  const repositories = Object.entries(REPOSITORIES).map(([repository, directory]) =>
    validateRepositoryAt(root, repository, directory));
  const indeterminate = repositories.some((item) => item.status === "INDETERMINATE");
  const failed = repositories.some((item) => item.status === "FAIL");
  return {
    schema_id: REPORT_SCHEMA_ID,
    status: indeterminate ? "INDETERMINATE" : failed ? "FAIL" : "PASS",
    repositories,
    errors: [],
  };
}

function parseArguments(argv) {
  const defaultRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
  if (argv.length === 0) return { workspaceRoot: defaultRoot, repository: null };
  if (argv.length === 2 && argv[0] === "--workspace-root") {
    return { workspaceRoot: path.resolve(argv[1]), repository: null };
  }
  if (argv.length === 2 && argv[0] === "--repo" && argv[1] === "worker") {
    return { workspaceRoot: defaultRoot, repository: "worker" };
  }
  throw new Error(
    "usage: check-current-reviewer-target.mjs [--workspace-root PATH | --repo worker]",
  );
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const options = parseArguments(process.argv.slice(2));
    const report = options.repository
      ? validateRepository(options.workspaceRoot, options.repository)
      : validateWorkspace(options.workspaceRoot);
    process.stdout.write(JSON.stringify(report) + "\n");
    process.exitCode = report.status === "PASS" ? 0 : report.status === "FAIL" ? 1 : 2;
  } catch (error) {
    process.stderr.write(String(error.message) + "\n");
    process.exitCode = 2;
  }
}
