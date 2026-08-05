import { readFile } from "node:fs/promises";
import {
  compareIanaRegistrySnapshot,
  parseIanaRegistryXml,
} from "./iana-registry.mjs";

const snapshot = JSON.parse(await readFile(
  new URL("../config/iana-special-purpose-registry.json", import.meta.url),
  "utf8",
));

const officialRegistries = {};
for (const family of ["ipv4", "ipv6"]) {
  const response = await fetch(snapshot.sources[family], {
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`IANA ${family} registry returned status ${response.status}`);
  officialRegistries[family] = parseIanaRegistryXml(family, await response.text());
}

const failures = compareIanaRegistrySnapshot(snapshot, officialRegistries);
if (failures.length > 0) {
  throw new Error(`IANA special-purpose registry drift detected:\n${failures.join("\n")}`);
}

console.log(
  `IANA special-purpose registry snapshot is current (IPv4 ${snapshot.registryVersions.ipv4}, IPv6 ${snapshot.registryVersions.ipv6})`,
);
