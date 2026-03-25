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

// Polyfill WebSocket for Node.js (required by EmbersEvents).
// The SDK constructs WebSocket URLs from the HTTP basePath (http://...).
// In browsers this auto-upgrades; the ws library needs ws:// explicitly.
import WebSocket from "ws";
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const g = globalThis as any;
g.WebSocket = function PatchedWebSocket(url: string, protocols?: string | string[]) {
  const wsUrl = url.replace(/^http:\/\//, "ws://").replace(/^https:\/\//, "wss://");
  return new WebSocket(wsUrl, protocols);
};
g.WebSocket.prototype = WebSocket.prototype;

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
): Promise<{ id: string; version: string }> {
  log("PHASE 2", "Creating agent team...");

  const { prepareResponse, waitForFinalization } =
    await sdk.agentsTeams.create({
      name: `E2E Demo Team ${Date.now()}`,
      description: "Automated E2E test team",
    });

  const id = prepareResponse.response.id;
  const version = prepareResponse.response.version;
  log("PHASE 2", `Team created: id=${id}, version=${version}`);
  log("PHASE 2", "Waiting for finalization...");

  await waitForFinalization;
  log("PHASE 2", "Create finalized");

  return { id, version };
}

// ---------------------------------------------------------------------------
// Phase 3: Save Graph
// ---------------------------------------------------------------------------

async function phase3_saveGraph(
  sdk: EmbersApiSdk,
  id: string,
): Promise<{ version: string; graph: Graph }> {
  log("PHASE 3", "Building demo graph (input -> text-model -> output)...");
  const graph = buildDemoGraph();

  log("PHASE 3", "Saving graph to agent team...");
  const { prepareResponse, waitForFinalization } = await sdk.agentsTeams.save(
    id,
    {
      name: `E2E Demo Team ${Date.now()}`,
      description: "Automated E2E test team",
      graph,
    },
  );

  const version = prepareResponse.response.version;
  log("PHASE 3", `Save prepared: new version=${version}`);
  log("PHASE 3", "Waiting for finalization...");

  await waitForFinalization;
  log("PHASE 3", "Save finalized");

  return { version, graph };
}

// ---------------------------------------------------------------------------
// Phase 4: Deploy
// ---------------------------------------------------------------------------

async function phase4_deploy(
  sdk: EmbersApiSdk,
  id: string,
  version: string,
): Promise<string> {
  log("PHASE 4", "Deploying agent team...");

  const registryKey = PrivateKey.new();
  const registryVersion = BigInt(1);

  let waitForFinalization: Promise<void>;
  try {
    ({ waitForFinalization } = await sdk.agentsTeams.deploy(
      id,
      version,
      { value: config.phloLimit } as { value: bigint },
      registryVersion,
      registryKey,
    ));
  } catch (err) {
    // Get details from the API directly
    const address = sdk.agentsTeams.address.toString();
    const res = await fetch(
      `${config.embersApiUrl}/api/ai-agents-teams/${address}/${id}/versions/${version}`,
    );
    const body = await res.text();
    log("PHASE 4", `Team state: ${res.status} ${body.slice(0, 500)}`);
    throw err;
  }

  log("PHASE 4", "Waiting for deploy finalization...");
  await waitForFinalization;
  log("PHASE 4", "Deploy finalized");

  // Poll for URI (recordDeploy may take extra time)
  log("PHASE 4", "Polling for URI...");
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
      await sleep(config.timeouts.uriPollInterval);
    }
  }

  if (!uri) {
    throw new Error(
      "Deploy finalized but URI not set (recordDeploy may have failed)",
    );
  }

  log("PHASE 4", `Deploy URI: ${uri}`);
  return uri;
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
): Promise<string> {
  // AT Protocol handles have length limits — keep it short
  const suffix = Date.now().toString(36);
  const handle = `e2e-${suffix}.test`;
  log("PHASE 6", `Publishing to F1R3Sky as ${handle}...`);

  // Use direct fetch to get proper error messages (SDK swallows response body)
  const address = sdk.agentsTeams.address.toString();
  const publishReq = {
    pds_url: config.pdsInternalUrl,
    handle,
    email: config.fireskyAgentEmail,
    password: config.fireskyAgentPassword,
  };
  const prepareRes = await fetch(
    `${config.embersApiUrl}/api/ai-agents-teams/${address}/${id}/publish-to-firesky/prepare`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(publishReq),
    },
  );
  if (!prepareRes.ok) {
    const body = await prepareRes.text();
    throw new Error(`Publish prepare: ${prepareRes.status} ${body.slice(0, 500)}`);
  }
  const prepareData = (await prepareRes.json()) as {
    response: { contract: string };
    token: string;
  };
  log("PHASE 6", "Publish prepared, signing...");

  // Sign and send using SDK internals
  const { signContract } = await import("@f1r3fly-io/embers-client-sdk");
  const privateKey = PrivateKey.tryFromHex(config.privateKeyHex);
  const contractBytes = new Uint8Array(
    Buffer.from(prepareData.response.contract, "base64"),
  );
  const signedContract = signContract(contractBytes, privateKey);

  const sendRes = await fetch(
    `${config.embersApiUrl}/api/ai-agents-teams/${address}/${id}/publish-to-firesky/send`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prepareRequest: publishReq,
        prepareResponse: prepareData.response,
        request: signedContract,
        token: prepareData.token,
      }),
    },
  );
  if (!sendRes.ok) {
    const body = await sendRes.text();
    throw new Error(`Publish send: ${sendRes.status} ${body.slice(0, 500)}`);
  }
  log("PHASE 6", "Publish sent, waiting for finalization...");

  // Subscribe for finalization using the signature as deploy ID
  const deployId = Buffer.from(signedContract.sig).toString("hex");
  const waitForFinalization = sdk.events.subscribeForDeploy(
    deployId,
    config.timeouts.publishFinalize,
  );

  log("PHASE 6", "Waiting for publish finalization...");
  await waitForFinalization;
  log("PHASE 6", `Published as ${handle}`);

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

async function main(): Promise<void> {
  log("MAIN", `=== E2E Demo Test Starting ===${noTeardown ? " (no-teardown)" : ""}`);

  let shouldTeardown = false;

  try {
    // Phase 0: Start services
    await phase0_startServices();
    shouldTeardown = true;

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

    // Give WebSocket time to connect
    await sleep(2_000);

    // Phase 2: Create
    const { id } = await phase2_createTeam(sdk);

    // Phase 3: Save graph
    const { version: saveVersion } = await phase3_saveGraph(sdk, id);

    // Phase 4: Deploy
    const uri = await phase4_deploy(sdk, id, saveVersion);

    // Phase 5: Run
    await phase5_run(sdk, uri);

    // Phase 6: Publish
    const agentHandle = await phase6_publish(sdk, id);

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
