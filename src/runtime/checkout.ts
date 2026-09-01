import { spawn } from "node:child_process";
import { lstat, mkdir, realpath, rm } from "node:fs/promises";
import path from "node:path";

const REPARSE_POINT = 0x400;

export interface CheckoutJob {
  readonly job_id: string;
  readonly run_id: string;
  readonly repository?: {
    readonly clone_url?: string;
    readonly commit_sha?: string;
  };
  readonly clone_token?: { readonly token?: string } | null;
}

export interface RunCommandOptions {
  readonly cwd: string;
  readonly env: Readonly<Record<string, string>>;
  readonly signal?: AbortSignal;
}

export type RunCommand = (
  command: string,
  args: readonly string[],
  options: RunCommandOptions,
) => Promise<void>;

export interface MaterializeOptions {
  readonly checkoutRoot: string;
  readonly runCommand?: RunCommand;
  readonly signal?: AbortSignal;
}

function safeId(value: unknown, label: string): string {
  const text = String(value ?? "").trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u.test(text)) {
    throw new TypeError(`${label} is invalid`);
  }
  return text;
}

function contained(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

async function safeRoot(root: string): Promise<string> {
  const lexical = path.resolve(root);
  await mkdir(lexical, { recursive: true, mode: 0o700 });
  const metadata = await lstat(lexical);
  const resolved = await realpath(lexical);
  const attributes = (metadata as typeof metadata & { fileAttributes?: number }).fileAttributes ?? 0;
  if (
    !metadata.isDirectory() ||
    metadata.isSymbolicLink() ||
    Boolean(attributes & REPARSE_POINT) ||
    !contained(lexical, resolved) ||
    !contained(resolved, lexical)
  ) {
    throw new Error("checkout root must be a real, non-linked directory");
  }
  return resolved;
}

const runCommand: RunCommand = (command, args, options) => new Promise((resolve, reject) => {
  if (options.signal?.aborted) {
    reject(new Error("checkout cancelled"));
    return;
  }
  const child = spawn(command, [...args], {
    cwd: options.cwd,
    env: { ...process.env, ...options.env },
    shell: false,
    windowsHide: true,
    detached: process.platform !== "win32",
    stdio: ["ignore", "ignore", "pipe"],
  });
  let stderr = "";
  child.stderr?.on("data", (chunk) => {
    if (stderr.length < 64 * 1024) stderr += String(chunk);
  });
  const abort = () => {
    if (child.pid && process.platform !== "win32") {
      try { process.kill(-child.pid, "SIGTERM"); } catch { child.kill("SIGTERM"); }
    } else {
      child.kill("SIGTERM");
    }
  };
  options.signal?.addEventListener("abort", abort, { once: true });
  child.once("error", (error) => {
    options.signal?.removeEventListener("abort", abort);
    reject(error);
  });
  child.once("close", (code) => {
    options.signal?.removeEventListener("abort", abort);
    if (options.signal?.aborted) reject(new Error("checkout cancelled"));
    else if (code === 0) resolve();
    else reject(new Error(`git exited with ${code}: ${stderr.trim()}`));
  });
});

export async function materializeCheckout(job: CheckoutJob, options: MaterializeOptions) {
  const root = await safeRoot(options.checkoutRoot);
  const jobId = safeId(job.job_id, "job_id");
  const runId = safeId(job.run_id, "run_id");
  const cloneUrl = new URL(String(job.repository?.clone_url ?? ""));
  if (cloneUrl.protocol !== "https:" || cloneUrl.hostname !== "github.com" || cloneUrl.username || cloneUrl.password) {
    throw new TypeError("repository clone URL must be credential-free HTTPS github.com");
  }
  const commit = String(job.repository?.commit_sha ?? "").trim().toLowerCase();
  if (!/^[0-9a-f]{7,64}$/u.test(commit)) throw new TypeError("repository commit SHA is invalid");
  const token = String(job.clone_token?.token ?? "").trim();
  if (!token || /[\r\n]/u.test(token)) throw new TypeError("clone token is missing or invalid");

  const attemptRoot = path.join(root, `${jobId}-${runId}`);
  const workspace = path.join(attemptRoot, "repository");
  if (!contained(root, attemptRoot)) throw new Error("checkout path escapes root");
  await mkdir(attemptRoot, { recursive: false, mode: 0o700 });
  const execute = options.runCommand ?? runCommand;
  const authEnv = {
    GIT_CONFIG_COUNT: "1",
    GIT_CONFIG_KEY_0: "http.extraHeader",
    GIT_CONFIG_VALUE_0: `Authorization: Bearer ${token}`,
    GIT_TERMINAL_PROMPT: "0",
  };
  try {
    await execute(
      "git",
      ["clone", "--no-checkout", "--filter=blob:none", "--", cloneUrl.href, workspace],
      { cwd: attemptRoot, env: authEnv, ...(options.signal ? { signal: options.signal } : {}) },
    );
    await execute(
      "git",
      ["-C", workspace, "checkout", "--detach", commit],
      { cwd: attemptRoot, env: { GIT_TERMINAL_PROMPT: "0" }, ...(options.signal ? { signal: options.signal } : {}) },
    );
    return {
      workspace,
      cleanup: async () => {
        if (!contained(root, attemptRoot)) throw new Error("unsafe checkout cleanup path");
        await rm(attemptRoot, { recursive: true, force: true });
      },
    };
  } catch (error) {
    await rm(attemptRoot, { recursive: true, force: true });
    throw error;
  }
}
