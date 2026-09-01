import { lstat, readFile, realpath } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

import type { ReviewAttempt } from "./attempt-supervisor.ts";

export interface ReviewCapability {
  readonly root: string;
  readonly systemPrompt: string;
  readonly contextText: string;
  readonly referenceText: string;
  readonly promptTemplate: string;
  readonly skill: {
    readonly name: string;
    readonly description: string;
    readonly filePath: string;
    readonly baseDir: string;
    readonly content: string;
  };
}

const DEFAULT_ROOT = fileURLToPath(new URL("../../reviewer", import.meta.url));
const REPARSE_POINT = 0x400;
const MAX_TEXT_BYTES = 256 * 1024;

async function readOwnedText(root: string, relativePath: string): Promise<{ path: string; text: string }> {
  const lexical = path.resolve(root, relativePath);
  if (path.relative(root, lexical).startsWith("..") || path.isAbsolute(path.relative(root, lexical))) {
    throw new Error(`capability path escapes root: ${relativePath}`);
  }
  const metadata = await lstat(lexical);
  const resolved = await realpath(lexical);
  if (
    !metadata.isFile() ||
    metadata.isSymbolicLink() ||
    Boolean(((metadata as typeof metadata & { fileAttributes?: number }).fileAttributes ?? 0) & REPARSE_POINT) ||
    path.relative(lexical, resolved) !== "" ||
    metadata.size > MAX_TEXT_BYTES
  ) {
    throw new Error(`unsafe capability file: ${relativePath}`);
  }
  const bytes = await readFile(resolved);
  return { path: resolved, text: new TextDecoder("utf-8", { fatal: true }).decode(bytes) };
}

function parseSkillFrontmatter(content: string): { name: string; description: string } {
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n/u);
  if (!match?.[1]) throw new Error("review skill is missing frontmatter");
  const fields = new Map<string, string>();
  for (const line of match[1].split(/\r?\n/u)) {
    const separator = line.indexOf(":");
    if (separator > 0) fields.set(line.slice(0, separator).trim(), line.slice(separator + 1).trim());
  }
  const name = fields.get("name") ?? "";
  const description = fields.get("description") ?? "";
  if (!/^[a-z0-9-]+$/u.test(name) || !description) {
    throw new Error("review skill frontmatter is invalid");
  }
  return { name, description };
}

export async function loadReviewCapability(root = DEFAULT_ROOT): Promise<ReviewCapability> {
  const lexicalRoot = path.resolve(root);
  const rootMetadata = await lstat(lexicalRoot);
  const resolvedRoot = await realpath(lexicalRoot);
  if (!rootMetadata.isDirectory() || rootMetadata.isSymbolicLink() || path.relative(lexicalRoot, resolvedRoot)) {
    throw new Error("review capability root must be a real directory");
  }
  const [system, context, reference, prompt, skillFile] = await Promise.all([
    readOwnedText(resolvedRoot, "system.md"),
    readOwnedText(resolvedRoot, "context.md"),
    readOwnedText(resolvedRoot, "references/review-method.md"),
    readOwnedText(resolvedRoot, "prompts/review-repository.md"),
    readOwnedText(resolvedRoot, "skills/pullwise-repository-review/SKILL.md"),
  ]);
  if (prompt.text.split("{{ATTEMPT_CONTEXT_JSON}}").length !== 2) {
    throw new Error("review prompt must contain exactly one attempt context placeholder");
  }
  const metadata = parseSkillFrontmatter(skillFile.text);
  return Object.freeze({
    root: resolvedRoot,
    systemPrompt: system.text,
    contextText: context.text,
    referenceText: reference.text,
    promptTemplate: prompt.text,
    skill: Object.freeze({
      ...metadata,
      filePath: skillFile.path,
      baseDir: path.dirname(skillFile.path),
      content: skillFile.text,
    }),
  });
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson((value as Record<string, unknown>)[key])}`).join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw new TypeError("attempt context contains a non-JSON value");
  return encoded;
}

export function renderReviewPrompt(attempt: ReviewAttempt, capability: ReviewCapability): string {
  const context = canonicalJson({ attempt_id: attempt.attemptId, request: attempt.context });
  return `/skill:${capability.skill.name}\n\n${capability.promptTemplate.replace(
    "{{ATTEMPT_CONTEXT_JSON}}",
    context,
  )}`;
}
