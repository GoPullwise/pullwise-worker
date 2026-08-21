#!/usr/bin/env node

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
  if (
    occurrences(normalized, TARGET_START) !== 1 ||
    occurrences(normalized, TARGET_END) !== 1
  ) {
    errors.push("target_block_count_mismatch");
    return errors;
  }
  const targetStart = normalized.indexOf(TARGET_START);
  const targetEnd = normalized.indexOf(TARGET_END, targetStart);
  const actual = normalized.slice(targetStart, targetEnd + TARGET_END.length);
  if (actual !== TARGET_BLOCK) {
    errors.push("target_block_mismatch");
  }
  const expectedOffset = currentEnd + CURRENT_END.length + 1;
  if (targetStart !== expectedOffset) {
    errors.push("target_block_not_immediately_after_authority");
  }
  return [...new Set(errors)].sort();
}

function isLink(metadata) {
  const reparsePoint = 0x400;
  return metadata.isSymbolicLink() ||
    Boolean((metadata.mode & 0o170000) === 0o120000) ||
    Boolean(metadata.birthtimeMs !== undefined &&
      metadata.isFile() &&
      metadata.mode === 0 &&
      reparsePoint);
}

function validateRepository(workspaceRoot, repository, directory) {
  const repositoryRoot = path.join(workspaceRoot, directory);
  const agentsPath = path.join(repositoryRoot, "AGENTS.md");
  try {
    const rootMetadata = fs.lstatSync(repositoryRoot);
    if (!rootMetadata.isDirectory() || isLink(rootMetadata)) {
      throw new Error("repository_path_not_safe");
    }
    const metadata = fs.lstatSync(agentsPath);
    if (!metadata.isFile() || isLink(metadata)) {
      throw new Error("agents_file_not_regular");
    }
    const raw = fs.readFileSync(agentsPath);
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
      "agents_file_not_regular",
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

export function validateWorkspace(workspaceRoot) {
  const root = path.resolve(workspaceRoot);
  let rootMetadata;
  try {
    rootMetadata = fs.lstatSync(root);
  } catch {
    return {
      schema_id: REPORT_SCHEMA_ID,
      status: "INDETERMINATE",
      repositories: [],
      errors: ["workspace_root_unreadable"],
    };
  }
  if (!rootMetadata.isDirectory() || isLink(rootMetadata)) {
    return {
      schema_id: REPORT_SCHEMA_ID,
      status: "INDETERMINATE",
      repositories: [],
      errors: ["workspace_root_not_safe"],
    };
  }
  const repositories = Object.entries(REPOSITORIES).map(([repository, directory]) =>
    validateRepository(root, repository, directory));
  const indeterminate = repositories.some((item) => item.status === "INDETERMINATE");
  const failed = repositories.some((item) => item.status === "FAIL");
  return {
    schema_id: REPORT_SCHEMA_ID,
    status: indeterminate ? "INDETERMINATE" : failed ? "FAIL" : "PASS",
    repositories,
    errors: [],
  };
}

function parseWorkspaceRoot(argv) {
  const defaultRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
  if (argv.length === 0) return defaultRoot;
  if (argv.length === 2 && argv[0] === "--workspace-root") return path.resolve(argv[1]);
  throw new Error("usage: check-current-reviewer-target.mjs [--workspace-root PATH]");
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const report = validateWorkspace(parseWorkspaceRoot(process.argv.slice(2)));
    process.stdout.write(JSON.stringify(report) + "\n");
    process.exitCode = report.status === "PASS" ? 0 : report.status === "FAIL" ? 1 : 2;
  } catch (error) {
    process.stderr.write(String(error.message) + "\n");
    process.exitCode = 2;
  }
}
