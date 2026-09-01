import { lstat, readFile, realpath } from "node:fs/promises";
import path from "node:path";
import { TextDecoder } from "node:util";

const REPARSE_POINT = 0x400;
const MAX_FENCE_BYTES = 512;

export interface FileFence {
  readonly relativePath: string;
  readonly expected: string;
}

function attributes(metadata: Awaited<ReturnType<typeof lstat>>): number {
  return (metadata as typeof metadata & { fileAttributes?: number }).fileAttributes ?? 0;
}

export async function createFileFenceValidator(
  root: string,
  fence: FileFence,
): Promise<() => Promise<boolean>> {
  if (!fence.expected || fence.expected.length > 256 || /[\r\n]/u.test(fence.expected)) {
    throw new TypeError("fence expected value must be one non-empty line of at most 256 characters");
  }
  if (
    !fence.relativePath ||
    path.isAbsolute(fence.relativePath) ||
    fence.relativePath.split(/[\\/]/u).some((segment) => !segment || segment === "." || segment === "..")
  ) {
    throw new TypeError("fence path must be a contained relative file path");
  }
  const lexicalRoot = path.resolve(root);
  const rootMetadata = await lstat(lexicalRoot);
  const resolvedRoot = await realpath(lexicalRoot);
  if (
    !rootMetadata.isDirectory() ||
    rootMetadata.isSymbolicLink() ||
    Boolean(attributes(rootMetadata) & REPARSE_POINT) ||
    path.relative(lexicalRoot, resolvedRoot) !== ""
  ) {
    throw new Error("fence root must be a real, non-linked directory");
  }
  const lexicalFile = path.resolve(resolvedRoot, fence.relativePath);
  if (path.relative(resolvedRoot, lexicalFile).startsWith("..")) {
    throw new TypeError("fence path escapes its root");
  }

  return async () => {
    try {
      const before = await lstat(lexicalFile);
      if (
        !before.isFile() ||
        before.isSymbolicLink() ||
        Boolean(attributes(before) & REPARSE_POINT) ||
        before.size > MAX_FENCE_BYTES ||
        path.relative(lexicalFile, await realpath(lexicalFile)) !== ""
      ) {
        return false;
      }
      const bytes = await readFile(lexicalFile);
      const after = await lstat(lexicalFile);
      if (before.dev !== after.dev || before.ino !== after.ino || before.size !== after.size) {
        return false;
      }
      const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      return text === fence.expected || text === `${fence.expected}\n`;
    } catch {
      return false;
    }
  };
}
