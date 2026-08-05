import { readFile, readdir } from "node:fs/promises";
import { join } from "node:path";
import { gzipSync } from "node:zlib";
import { assertProductionBundleBudget } from "./production-bundle-budget.mjs";

const forbidden = [
  "preview-lease",
  "Run the focused Android control tests and fix the first real failure.",
  "hermes.access_token",
  "sessionStorage",
];

const files = await collectFiles("dist/client");
const textFiles = files.filter((file) => /\.(?:html|js|css|json)$/.test(file));
for (const file of textFiles) {
  const content = await readFile(file, "utf8");
  for (const sentinel of forbidden) {
    if (content.includes(sentinel)) {
      throw new Error(`Production bundle contains forbidden development or credential sentinel: ${sentinel}`);
    }
  }
}

const javascriptFiles = files.filter((file) => /\.js$/.test(file));
const stylesheetFiles = files.filter((file) => /\.css$/.test(file));
const javascript = await measure(javascriptFiles);
const stylesheets = await measure(stylesheetFiles);
assertProductionBundleBudget({
  assetCount: files.length,
  jsRawBytes: javascript.rawBytes,
  jsGzipBytes: javascript.gzipBytes,
  cssRawBytes: stylesheets.rawBytes,
  cssGzipBytes: stylesheets.gzipBytes,
});

console.log("Production bundle gate passed");

async function measure(paths) {
  let rawBytes = 0;
  let gzipBytes = 0;
  for (const path of paths) {
    const content = await readFile(path);
    rawBytes += content.byteLength;
    gzipBytes += gzipSync(content, { level: 9 }).byteLength;
  }
  return { rawBytes, gzipBytes };
}

async function collectFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await collectFiles(path));
    else files.push(path);
  }
  return files;
}
