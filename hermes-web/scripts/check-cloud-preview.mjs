import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import {
  createVitePreviewArguments,
  stopProcess,
  waitForPreviewOrigin,
  waitForPreviewReady,
} from "./cloud-preview-runtime.mjs";

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const viteCli = join(projectRoot, "node_modules", "vite", "bin", "vite.js");
const previewProcess = spawn(process.execPath, createVitePreviewArguments(viteCli), {
  cwd: projectRoot,
  env: process.env,
  stdio: ["ignore", "pipe", "pipe"],
});
let previewOutput = "";
previewProcess.stdout.on("data", (chunk) => { previewOutput = appendBounded(previewOutput, chunk); });
previewProcess.stderr.on("data", (chunk) => { previewOutput = appendBounded(previewOutput, chunk); });

let browser;
let primaryFailure;
try {
  const origin = await waitForPreviewOrigin(previewProcess);
  await waitForPreviewReady(origin, previewProcess);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto(origin, { waitUntil: "networkidle" });
  await page.getByLabel("Username").waitFor({ state: "visible" });
  await page.getByLabel("Password").waitFor({ state: "visible" });
  await page.getByLabel("Session key").waitFor({ state: "visible" });
  await page.getByRole("button", { name: "Sign in" }).waitFor({ state: "visible" });

  const healthResponse = await fetch(`${origin}/live`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!healthResponse.ok) {
    throw new Error(`Cloud liveness proxy returned status ${healthResponse.status}`);
  }
  if (!(healthResponse.headers.get("content-type") ?? "").includes("application/json")) {
    throw new Error("Cloud liveness proxy returned a non-JSON response");
  }
} catch (error) {
  primaryFailure = error;
}

const cleanupResults = await Promise.allSettled([
  browser?.close(),
  stopProcess(previewProcess),
]);
const cleanupFailures = cleanupResults.flatMap((result) => (
  result.status === "rejected" ? [result.reason] : []
));
if (primaryFailure !== undefined || cleanupFailures.length > 0) {
  const failures = [
    ...(primaryFailure === undefined ? [] : [primaryFailure]),
    ...cleanupFailures,
  ];
  const detail = previewOutput.trim();
  throw new AggregateError(
    failures,
    detail.length > 0 ? `Cloud preview smoke failed\n${detail}` : "Cloud preview smoke failed",
  );
}

console.log("Cloud preview smoke passed: production login rendered and /live reached Cloud");

function appendBounded(current, chunk) {
  return `${current}${String(chunk)}`.slice(-4_000);
}
