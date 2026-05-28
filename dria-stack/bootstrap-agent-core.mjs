import { spawnSync } from "node:child_process";
import { cp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const target = path.join(here, "agent-core");
const repo = process.env.FIRECRAWL_WEB_AGENT_REPO || "https://github.com/firecrawl/web-agent.git";
const ref = process.env.FIRECRAWL_WEB_AGENT_REF || "f023adf1cd1f731e27fdc844af62996f6c2a41c4";

async function exists(filePath) {
  try {
    await stat(filePath);
    return true;
  } catch {
    return false;
  }
}

function run(command, args, cwd) {
  const result = spawnSync(command, args, {
    cwd,
    stdio: "inherit",
    shell: process.platform === "win32",
  });
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(" ")} failed with ${result.status}`);
  }
}

async function patchAgentCore() {
  const agentPath = path.join(target, "src", "agent.ts");
  if (!(await exists(agentPath))) return;
  let source = await readFile(agentPath, "utf8");
  source = source.replace(
    '  const modelName = `${config.provider}:${config.model}`;\n',
    [
      '  const provider = config.provider === "custom-openai" ? "openai" : config.provider;',
      "  const modelName = `${provider}:${config.model}`;",
      "",
    ].join("\n"),
  );
  if (!source.includes("DRIA_AGENT_MAX_OUTPUT_TOKENS")) {
    source = source.replace(
      "  if (config.baseURL) opts.configuration = { baseURL: config.baseURL };\n",
      [
        "  if (config.baseURL) opts.configuration = { baseURL: config.baseURL };",
        '  if (config.provider === "custom-openai") {',
        '    const maxTokens = Number(process.env.DRIA_AGENT_MAX_OUTPUT_TOKENS ?? "1024");',
        "    if (Number.isFinite(maxTokens) && maxTokens > 0) opts.maxTokens = maxTokens;",
        "  }",
        "",
      ].join("\n"),
    );
  }
  await writeFile(agentPath, source, "utf8");
}

if (await exists(path.join(target, "src", "index.ts"))) {
  await patchAgentCore();
  console.log(`agent-core already exists at ${target}`);
  process.exit(0);
}

const tmp = path.join(tmpdir(), `dria-web-agent-${Date.now()}`);
await rm(tmp, { recursive: true, force: true });
await mkdir(tmp, { recursive: true });

try {
  run("git", ["clone", "--filter=blob:none", "--sparse", repo, tmp], undefined);
  run("git", ["checkout", ref], tmp);
  run("git", ["sparse-checkout", "set", "agent-core"], tmp);
  await rm(target, { recursive: true, force: true });
  await cp(path.join(tmp, "agent-core"), target, { recursive: true });
  await patchAgentCore();
  console.log(`agent-core bootstrapped at ${target}`);
} finally {
  await rm(tmp, { recursive: true, force: true });
}
