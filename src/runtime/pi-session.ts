import path from "node:path";

import {
  createAgentSession,
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  SettingsManager,
  type Skill,
} from "@earendil-works/pi-coding-agent";

import type {
  ReviewAttempt,
  ReviewSession,
  ReviewSessionEvent,
  ReviewUsage,
} from "./attempt-supervisor.ts";
import type { ReviewCapability } from "./review-capability.ts";
import { createContainedReviewTools } from "./contained-tools.ts";

export const READ_ONLY_TOOLS = Object.freeze(["repo_read", "repo_grep", "repo_ls"] as const);

interface PiStats {
  readonly tokens: {
    readonly input: number;
    readonly output: number;
    readonly cacheRead: number;
    readonly cacheWrite: number;
    readonly total: number;
  };
  readonly cost: number;
}

type PiPortEvent = { readonly type: string; readonly message?: { readonly role?: string } };

export interface PiSessionPort {
  readonly sessionId: string;
  readonly model: { readonly provider: string; readonly id: string };
  readonly thinkingLevel: string;
  subscribe(listener: (event: PiPortEvent) => void): () => void;
  prompt(text: string): Promise<void>;
  abort(): Promise<void>;
  getLastAssistantText(): string | undefined;
  getSessionStats(): PiStats;
  dispose(): void;
}

export function assertExactPiRuntime(
  attempt: Pick<ReviewAttempt, "provider" | "model" | "thinkingLevel">,
  session: Pick<PiSessionPort, "model" | "thinkingLevel">,
): void {
  if (
    session.model?.provider !== attempt.provider ||
    session.model.id !== attempt.model
  ) {
    throw new Error("Pi did not preserve the exact configured provider/model identity");
  }
  if (session.thinkingLevel !== attempt.thinkingLevel) {
    throw new Error("Pi did not preserve the exact configured thinking level");
  }
}

function mapUsage(stats: PiStats): ReviewUsage {
  return Object.freeze({
    input: stats.tokens.input,
    output: stats.tokens.output,
    cacheRead: stats.tokens.cacheRead,
    cacheWrite: stats.tokens.cacheWrite,
    total: stats.tokens.total,
    cost: stats.cost,
  });
}

export class PiReviewSession implements ReviewSession {
  readonly sessionId: string;
  readonly model: PiSessionPort["model"];
  private readonly listeners = new Set<(event: ReviewSessionEvent) => void>();
  private readonly unsubscribePi: () => void;
  private readonly session: PiSessionPort;
  private disposed = false;

  constructor(session: PiSessionPort) {
    this.session = session;
    this.sessionId = session.sessionId;
    this.model = Object.freeze({ ...session.model });
    this.unsubscribePi = session.subscribe((event) => {
      if (event.type === "message_end" && event.message?.role === "assistant") {
        const usageEvent: ReviewSessionEvent = { type: "usage", usage: this.getUsage() };
        for (const listener of this.listeners) listener(usageEvent);
      }
    });
  }

  subscribe(listener: (event: ReviewSessionEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  prompt(text: string): Promise<void> {
    return this.session.prompt(text);
  }

  abort(): Promise<void> {
    return this.session.abort();
  }

  getLastAssistantText(): string | undefined {
    return this.session.getLastAssistantText();
  }

  getUsage(): ReviewUsage {
    return mapUsage(this.session.getSessionStats());
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.listeners.clear();
    this.unsubscribePi();
    this.session.dispose();
  }
}

export interface PiSessionFactoryOptions {
  readonly agentDir: string;
  readonly capability: ReviewCapability;
}

export function createPiReviewSessionFactory(options: PiSessionFactoryOptions) {
  return async (attempt: ReviewAttempt): Promise<ReviewSession> => {
    const settingsManager = SettingsManager.inMemory({
      compaction: { enabled: true },
      retry: { enabled: false },
      enableSkillCommands: true,
    }, { projectTrusted: false });
    const skill: Skill = {
      name: options.capability.skill.name,
      description: options.capability.skill.description,
      filePath: options.capability.skill.filePath,
      baseDir: options.capability.skill.baseDir,
      sourceInfo: {
        path: options.capability.skill.filePath,
        source: "pullwise-worker",
        scope: "temporary",
        origin: "top-level",
        baseDir: options.capability.skill.baseDir,
      },
      disableModelInvocation: false,
    };
    const resourceLoader = new DefaultResourceLoader({
      cwd: attempt.workspace,
      agentDir: path.resolve(options.agentDir),
      settingsManager,
      noExtensions: true,
      noThemes: true,
      systemPrompt: options.capability.systemPrompt,
      appendSystemPrompt: [options.capability.contextText, options.capability.referenceText],
      skillsOverride: ({ diagnostics }) => ({ skills: [skill], diagnostics }),
      promptsOverride: ({ diagnostics }) => ({ prompts: [], diagnostics }),
    });
    await resourceLoader.reload({ resolveProjectTrust: async () => false });

    const agentDir = path.resolve(options.agentDir);
    const modelRuntime = await ModelRuntime.create({
      authPath: path.join(agentDir, "auth.json"),
      modelsPath: path.join(agentDir, "models.json"),
      allowModelNetwork: false,
      refreshOnCreate: false,
    });
    const model = modelRuntime.getModel(attempt.provider, attempt.model);
    if (!model) throw new Error(`configured Pi model is unavailable: ${attempt.provider}/${attempt.model}`);
    const customTools = await createContainedReviewTools(attempt.workspace);
    const created = await createAgentSession({
      cwd: attempt.workspace,
      agentDir,
      modelRuntime,
      model,
      thinkingLevel: attempt.thinkingLevel,
      tools: [...READ_ONLY_TOOLS],
      customTools,
      resourceLoader,
      settingsManager,
      sessionManager: SessionManager.inMemory(attempt.workspace),
    });
    if (created.modelFallbackMessage) {
      created.session.dispose();
      throw new Error(`Pi model fallback is forbidden: ${created.modelFallbackMessage}`);
    }
    try {
      assertExactPiRuntime(
        attempt,
        created.session as unknown as Pick<PiSessionPort, "model" | "thinkingLevel">,
      );
    } catch (error) {
      created.session.dispose();
      throw error;
    }
    return new PiReviewSession(created.session as unknown as PiSessionPort);
  };
}
