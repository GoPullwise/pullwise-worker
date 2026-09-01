import {
  addProfile,
  buildRuntimeCatalog,
  loadProfiles,
} from "./runtime/profiles.ts";

interface ProfileCommandOptions {
  readonly profileRoot: string;
  readonly write: (text: string) => void;
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

function addOptions(args: string[]): { credentialId: string; label: string; provider: string; authType: "api_key" | "oauth" | "subscription" } {
  const values = new Map<string, string>();
  for (let index = 0; index < args.length; index += 2) {
    const option = args[index];
    const value = args[index + 1];
    if (!option || !["--id", "--label", "--provider", "--auth-type"].includes(option)) {
      throw new Error(`unknown option: ${option ?? ""}`);
    }
    if (!value || value.startsWith("--")) throw new Error(`${option} requires a value`);
    if (values.has(option)) throw new Error(`duplicate option: ${option}`);
    values.set(option, value);
  }
  const authType = values.get("--auth-type") ?? "api_key";
  if (!(["api_key", "oauth", "subscription"] as const).includes(authType as "api_key")) {
    throw new Error("--auth-type must be api_key, oauth, or subscription");
  }
  return {
    credentialId: values.get("--id") ?? "",
    label: values.get("--label") ?? "",
    provider: values.get("--provider") ?? "",
    authType: authType as "api_key" | "oauth" | "subscription",
  };
}

export async function runProfileCommand(
  args: string[],
  options: ProfileCommandOptions,
): Promise<number> {
  const command = args[0];
  if (command === "add") {
    const profile = await addProfile(options.profileRoot, addOptions(args.slice(1)));
    options.write(`${JSON.stringify({
      profile: {
        credentialId: profile.credentialId,
        label: profile.label,
        provider: profile.provider,
        authType: profile.authType,
        agentDir: profile.agentDir,
      },
      authCommand:
        `PI_CODING_AGENT_DIR=${shellQuote(profile.agentDir)} pi auth login --provider ${shellQuote(profile.provider)}`,
    })}\n`);
    return 0;
  }
  if (command === "list") {
    const profiles = await loadProfiles(options.profileRoot);
    options.write(`${JSON.stringify({
      profiles: profiles.profiles.map((profile) => ({
        credentialId: profile.credentialId,
        label: profile.label,
        provider: profile.provider,
        authType: profile.authType,
        agentDir: profile.agentDir,
      })),
    })}\n`);
    return 0;
  }
  if (command === "catalog") {
    const profiles = await loadProfiles(options.profileRoot);
    options.write(`${JSON.stringify(await buildRuntimeCatalog(profiles))}\n`);
    return 0;
  }
  throw new Error("usage: pullwise-worker profile add|list|catalog");
}
