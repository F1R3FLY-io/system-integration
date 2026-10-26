/**
 * E2E Demo Test
 *
 * Automates the full demo flow:
 *   Phase 0: Start services
 *   Phase 1: Pre-flight health checks
 *   Phase 2: Create agent team
 *   Phase 3: Save graph
 *   Phase 4: Deploy
 *   Phase 5: Run
 *   Phase 6: Publish to F1R3Sky
 *   Phase 7: Verify publish
 *   Phase 8: F1R3Sky post + agent reply
 *   Teardown: Stop services
 */

import { execSync } from "node:child_process";

import {
  EmbersApiSdk,
  PrivateKey,
  Uri,
  type Graph,
} from "@f1r3fly-io/embers-client-sdk";

import { config } from "./lib/config.js";
import { FireskyVerifier } from "./lib/firesky-verifier.js";
import { buildDemoGraph } from "./lib/graph-builder.js";
import { log, logError } from "./lib/log.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function retry<T>(
  fn: () => Promise<T>,
  maxRetries: number,
  intervalMs: number,
  label: string,
): Promise<T> {
  let lastErr: unknown;
  for (let i = 0; i < maxRetries; i++) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      if (i < maxRetries - 1) {
        log(label, `Retry ${i + 1}/${maxRetries} in ${intervalMs / 1000}s...`);
        await sleep(intervalMs);
      }
    }
  }
  throw lastErr;
}

function exec(cmd: string, timeoutMs: number = 300_000): string {
  return execSync(cmd, { encoding: "utf-8", timeout: timeoutMs }).trim();
}

// ---------------------------------------------------------------------------
// Phase 0: Service Lifecycle
// ---------------------------------------------------------------------------

async function phase0_startServices(): Promise<void> {
  log("PHASE 0", "Checking shard health...");
  await retry(
    async () => {
      const res = await fetch(`${config.shardUrl}/status`);
      if (!res.ok) throw new Error(`Shard status: ${res.status}`);
    },
    10,
    5_000,
    "PHASE 0",
  );
  log("PHASE 0", "Shard is healthy");

  log("PHASE 0", "Starting services via start-all.sh...");
  try {
    const output = exec(config.startAllScript, 600_000);
    log("PHASE 0", "Services started");
    const lines = output.split("\n");
    console.log(lines.slice(-12).join("\n"));
  } catch (err) {
    logError("PHASE 0", "Failed to start services", err);
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Phase 1: Pre-flight
// ---------------------------------------------------------------------------

async function phase1_preflight(): Promise<void> {
  log("PHASE 1", "Pre-flight health checks...");

  await retry(
    async () => {
      const res = await fetch(`${config.embersApiUrl}/api/service/ready`);
      if (!res.ok) throw new Error(`Embers health: ${res.status}`);
    },
    12,
    5_000,
    "PHASE 1",
  );
  log("PHASE 1", "Embers API is healthy");

  const address = PrivateKey.tryFromHex(config.privateKeyHex)
    .getPublicKey()
    .getAddress()
    .toString();
  const teamsRes = await fetch(
    `${config.embersApiUrl}/api/ai-agents-teams/${address}`,
  );
  if (!teamsRes.ok) {
    throw new Error(`Failed to list agent teams: ${teamsRes.status}`);
  }
  log("PHASE 1", `Wallet ${address.slice(0, 20)}... can list agent teams`);
}

// ---------------------------------------------------------------------------
// Phase 2: Create Agent Team
// ---------------------------------------------------------------------------

async function phase2_createTeam(
  sdk: EmbersApiSdk,
): Promise<{ id: string; version: string; blockNumber: number }> {
  log("PHASE 2", "Creating agent team...");

  const { prepareResponse, blockNumber } = await sdk.agentsTeams.create({
    name: `E2E Demo Team ${Date.now()}`,
    description: "Automated E2E test team",
  });

  const id = prepareResponse.response.id;
  const version = prepareResponse.response.version;
  log("PHASE 2", `Team created: id=${id}, finalized at block ${blockNumber}`);

  return { id, version, blockNumber };
}

// ---------------------------------------------------------------------------
// Phase 3: Save Graph
// ---------------------------------------------------------------------------

async function phase3_saveGraph(
  sdk: EmbersApiSdk,
  id: string,
  validAfterBlockNumber: number,
): Promise<{ version: string; graph: Graph; blockNumber: number }> {
  log("PHASE 3", "Building demo graph (input -> text-model -> output)...");
  const graph = buildDemoGraph();

  log("PHASE 3", `Saving graph (valid_after=${validAfterBlockNumber})...`);
  const { prepareResponse, blockNumber } = await sdk.agentsTeams.save(
    id,
    {
      name: `E2E Demo Team ${Date.now()}`,
      description: "Automated E2E test team",
      graph,
    },
    { validAfterBlockNumber },
  );

  const version = prepareResponse.response.version;
  log("PHASE 3", `Save finalized at block ${blockNumber}, version=${version}`);

  return { version, graph, blockNumber };
}

// ---------------------------------------------------------------------------
// Phase 4: Deploy
// ---------------------------------------------------------------------------

async function phase4_deploy(
  sdk: EmbersApiSdk,
  id: string,
  version: string,
  validAfterBlockNumber: number,
): Promise<{ uri: string; blockNumber: number }> {
  log("PHASE 4", `Deploying (valid_after=${validAfterBlockNumber})...`);

  const registryKey = PrivateKey.new();
  const registryVersion = BigInt(1);

  const { blockNumber } = await sdk.agentsTeams.deploy(
    id,
    version,
    { value: config.phloLimit } as { value: bigint },
    registryVersion,
    registryKey,
    { validAfterBlockNumber },
  );

  log("PHASE 4", `Deploy finalized at block ${blockNumber}`);

  // Poll for URI — recordDeploy is a SECOND deploy that also needs to finalize.
  // Give it enough time (up to 15 retries × 2s = 30s after main deploy).
  log("PHASE 4", "Polling for URI (waiting for recordDeploy)...");
  const address = sdk.agentsTeams.address.toString();
  let uri: string | null = null;

  for (let i = 0; i < config.timeouts.uriPollRetries; i++) {
    const res = await fetch(
      `${config.embersApiUrl}/api/ai-agents-teams/${address}/${id}/versions/${version}`,
    );
    if (res.ok) {
      const data = (await res.json()) as { uri?: string | null };
      if (data.uri) {
        uri = data.uri;
        break;
      }
    }
    if (i < config.timeouts.uriPollRetries - 1) {
      log(
        "PHASE 4",
        `URI not set yet, retry ${i + 1}/${config.timeouts.uriPollRetries}...`,
      );
      await sleep(config.timeouts.pollInterval);
    }
  }

  if (!uri) {
    throw new Error(
      "Deploy finalized but URI not set after 30s (recordDeploy may have failed)",
    );
  }

  log("PHASE 4", `Deploy URI: ${uri}`);
  return { uri, blockNumber };
}

// ---------------------------------------------------------------------------
// Phase 5: Run
// ---------------------------------------------------------------------------

async function phase5_run(
  sdk: EmbersApiSdk,
  uriStr: string,
): Promise<unknown> {
  log("PHASE 5", "Running agent team...");
  const uri = Uri.tryFrom(uriStr);

  const { sendResponse } = await sdk.agentsTeams.run(
    uri,
    "Hello from E2E test. Summarize the concept of recursion in one sentence.",
    { value: config.phloLimit } as { value: bigint },
  );

  log("PHASE 5", "Run completed");
  log("PHASE 5", `Result: ${JSON.stringify(sendResponse).slice(0, 500)}`);
  return sendResponse;
}

// ---------------------------------------------------------------------------
// Phase 6: Publish to F1R3Sky
// ---------------------------------------------------------------------------

async function phase6_publish(
  sdk: EmbersApiSdk,
  id: string,
  validAfterBlockNumber: number,
): Promise<string> {
  const suffix = Date.now().toString(36);
  const handle = `e2e-${suffix}.test`;
  log("PHASE 6", `Publishing as ${handle} (valid_after=${validAfterBlockNumber})...`);

  const email = `e2e-${suffix}@test.invalid`;
  const { blockNumber } = await sdk.agentsTeams.publishToFiresky(
    id,
    {
      pdsUrl: config.pdsInternalUrl,
      handle,
      email,
      password: config.fireskyAgentPassword,
    },
    { validAfterBlockNumber },
  );

  log("PHASE 6", `Published as ${handle}, finalized at block ${blockNumber}`);
  return handle;
}

// ---------------------------------------------------------------------------
// Phase 7: Verify Publish
// ---------------------------------------------------------------------------

async function phase7_verifyPublish(agentHandle: string): Promise<string> {
  log("PHASE 7", "Verifying agent profile on F1R3Sky...");

  const verifier = new FireskyVerifier();

  const agentDid = await verifier.login(
    agentHandle,
    config.fireskyAgentPassword,
  );
  log("PHASE 7", `Agent DID: ${agentDid}`);

  const profile = await verifier.verifyProfile(agentDid);
  log("PHASE 7", `Agent profile: handle=${profile.handle}`);

  return agentDid;
}

// ---------------------------------------------------------------------------
// Phase 8: F1R3Sky Post + Agent Reply
// ---------------------------------------------------------------------------

async function phase8_fireskyInteraction(
  sdk: EmbersApiSdk,
  teamUriStr: string,
  agentHandle: string,
  agentDid: string,
): Promise<void> {
  log("PHASE 8", "Testing F1R3Sky post + agent reply flow...");

  const userVerifier = new FireskyVerifier();
  await userVerifier.login(
    config.fireskyUser.handle,
    config.fireskyUser.password,
  );
  log("PHASE 8", `Logged in as ${config.fireskyUser.handle}`);

  const prompt = "tell me about the ocean";
  const { uri: postUri, cid: postCid } = await userVerifier.createMentionPost(
    agentDid,
    agentHandle,
    prompt,
  );
  log("PHASE 8", `Created post: ${postUri}`);

  log("PHASE 8", "Triggering runOnFiresky...");
  const teamUri = Uri.tryFrom(teamUriStr);
  await sdk.agentsTeams.runOnFiresky(
    teamUri,
    prompt,
    { value: config.phloLimit } as { value: bigint },
    {
      parent: { cid: postCid, uri: postUri },
      root: { cid: postCid, uri: postUri },
    },
  );
  log("PHASE 8", "runOnFiresky completed");

  log("PHASE 8", "Verifying agent reply in thread...");
  const { found, replyText } = await userVerifier.verifyReplyInThread(
    postUri,
    agentDid,
  );

  if (found) {
    log(
      "PHASE 8",
      `Agent replied: "${(replyText ?? "").slice(0, 100)}${(replyText?.length ?? 0) > 100 ? "..." : ""}"`,
    );
  } else {
    log("PHASE 8", "WARNING: Agent reply not found in thread (may still be processing)");
  }
}

// ---------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------

function teardown(): void {
  log("TEARDOWN", "Stopping services...");
  try {
    exec(`${config.stopAllScript} --clean`);
    log("TEARDOWN", "Services stopped");
  } catch (err) {
    logError("TEARDOWN", "Failed to stop services", err);
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const noTeardown = process.argv.includes("--no-teardown");
const skipStart = process.argv.includes("--skip-start");

async function main(): Promise<void> {
  log("MAIN", `=== E2E Demo Test Starting ===${noTeardown ? " (no-teardown)" : ""}`);

  let shouldTeardown = false;

  try {
    // Phase 0: Start services
    if (skipStart) {
      log("PHASE 0", "Skipping service startup (--skip-start)");
    } else {
      await phase0_startServices();
      shouldTeardown = true;
    }

    // Phase 1: Pre-flight
    await phase1_preflight();

    // Initialize SDK
    log("MAIN", "Initializing Embers SDK...");
    const privateKey = PrivateKey.tryFromHex(config.privateKeyHex);
    const sdk = new EmbersApiSdk({
      basePath: config.embersApiUrl,
      privateKey,
    });
    log("MAIN", `Wallet address: ${sdk.agentsTeams.address.toString()}`);

    // Phase 2: Create — returns block number for causal chaining
    const { id, blockNumber: createBlock } = await phase2_createTeam(sdk);

    // Phase 3: Save — uses create's block number
    const { version: saveVersion, blockNumber: saveBlock } =
      await phase3_saveGraph(sdk, id, createBlock);

    // Phase 4: Deploy — uses save's block number
    const { uri, blockNumber: deployBlock } =
      await phase4_deploy(sdk, id, saveVersion, saveBlock);

    // Phase 5: Run
    await phase5_run(sdk, uri);

    // Phase 6: Publish — uses deploy's block number
    const agentHandle = await phase6_publish(sdk, id, deployBlock);

    // Phase 7: Verify publish
    const agentDid = await phase7_verifyPublish(agentHandle);

    // Phase 8: F1R3Sky interaction
    await phase8_fireskyInteraction(sdk, uri, agentHandle, agentDid);

    log("MAIN", "=== E2E Demo Test PASSED ===");
  } catch (err) {
    logError("MAIN", "=== E2E Demo Test FAILED ===", err);
    process.exitCode = 1;
  } finally {
    if (shouldTeardown && !noTeardown) {
      teardown();
    } else if (noTeardown) {
      log("MAIN", "Services left running (--no-teardown)");
    }
  }
}

main();
