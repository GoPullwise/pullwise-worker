import { constants } from "node:fs";
import { access, lstat, readFile, readdir, realpath, stat } from "node:fs/promises";
import path from "node:path";

import {
  createGrepToolDefinition,
  createLsToolDefinition,
  createReadToolDefinition,
  type ToolDefinition,
} from "@earendil-works/pi-coding-agent";

const REPARSE_POINT = 0x400;

function outside(): never {
  throw new Error("path is outside the attempt workspace");
}

function contained(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

export async function createContainedReviewTools(
  workspace: string,
): Promise<Array<ToolDefinition<any, any, any>>> {
  const lexicalRoot = path.resolve(workspace);
  const rootMetadata = await lstat(lexicalRoot);
  const root = await realpath(lexicalRoot);
  const attributes = (rootMetadata as typeof rootMetadata & { fileAttributes?: number }).fileAttributes ?? 0;
  if (
    !rootMetadata.isDirectory() ||
    rootMetadata.isSymbolicLink() ||
    Boolean(attributes & REPARSE_POINT) ||
    !contained(lexicalRoot, root) ||
    !contained(root, lexicalRoot)
  ) {
    throw new Error("attempt workspace must be a real, non-linked directory");
  }

  const resolveContained = async (candidate: string): Promise<string> => {
    const lexical = path.resolve(candidate);
    if (!contained(root, lexical)) outside();
    const resolved = await realpath(lexical);
    if (!contained(root, resolved)) outside();
    return resolved;
  };
  const existsContained = async (candidate: string): Promise<boolean> => {
    const lexical = path.resolve(candidate);
    if (!contained(root, lexical)) outside();
    try {
      await resolveContained(lexical);
      return true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
      throw error;
    }
  };
  const rename = <T extends ToolDefinition<any, any, any>>(definition: T, name: string): T => ({
    ...definition,
    name,
    label: name,
    promptGuidelines: [
      ...(definition.promptGuidelines ?? []),
      "All paths are confined to the current repository snapshot.",
    ],
  }) as T;

  const readDefinition = createReadToolDefinition(root, {
    autoResizeImages: false,
    operations: {
      access: async (candidate) => access(await resolveContained(candidate), constants.R_OK),
      readFile: async (candidate) => readFile(await resolveContained(candidate)),
    },
  });
  const grepDefinition = createGrepToolDefinition(root, {
    operations: {
      isDirectory: async (candidate) => (await stat(await resolveContained(candidate))).isDirectory(),
      readFile: async (candidate) => readFile(await resolveContained(candidate), "utf8"),
    },
  });
  const lsDefinition = createLsToolDefinition(root, {
    operations: {
      exists: existsContained,
      stat: async (candidate) => stat(await resolveContained(candidate)),
      readdir: async (candidate) => readdir(await resolveContained(candidate)),
    },
  });
  return [
    rename(readDefinition, "repo_read"),
    rename(grepDefinition, "repo_grep"),
    rename(lsDefinition, "repo_ls"),
  ];
}
