import { isIP } from "node:net";

const FAMILIES = ["ipv4", "ipv6"];

export function parseIanaRegistryXml(family, xml) {
  assertFamily(family);
  const version = elementText(xml, "updated");
  if (version === undefined || version === "") {
    throw new Error(`IANA ${family} registry is missing its updated version`);
  }

  const recordBodies = [...xml.matchAll(/<record(?:\s[^>]*)?>([\s\S]*?)<\/record>/gi)]
    .map((match) => match[1]);
  const records = recordBodies.flatMap((body) => {
    const addressText = elementText(body, "address");
    if (addressText === undefined || addressText === "") {
      throw new Error(`IANA ${family} registry contains a record without an address`);
    }
    const globallyReachable = parseReachability(elementText(body, "global"), family, addressText);
    return addressText.split(",").map((cidr) => ({
      family,
      cidr: normalizeCidr(family, cidr),
      globallyReachable,
    }));
  }).sort(compareTuples);

  return {
    family,
    version,
    sourceRecordCount: recordBodies.length,
    records,
  };
}

export function compareIanaRegistrySnapshot(snapshot, officialRegistries) {
  const failures = [];
  for (const family of FAMILIES) {
    const official = officialRegistries[family];
    const vendoredVersion = snapshot.registryVersions?.[family];
    if (official.version !== vendoredVersion) {
      failures.push(`${family} version is ${official.version}, vendored ${vendoredVersion ?? "missing"}`);
    }
    if (official.sourceRecordCount !== snapshot.sourceRecordCounts?.[family]) {
      failures.push(
        `${family} source record count is ${official.sourceRecordCount}, vendored ${snapshot.sourceRecordCounts?.[family] ?? "missing"}`,
      );
    }

    const officialSignatures = official.records.map(tupleSignature).sort();
    let vendoredSignatures;
    try {
      vendoredSignatures = (snapshot[family] ?? []).map((record) => tupleSignature({
        family,
        cidr: normalizeCidr(family, record.cidr),
        globallyReachable: normalizeVendoredReachability(record.globallyReachable),
      })).sort();
    } catch (error) {
      failures.push(`${family} vendored tuple is invalid: ${error.message}`);
      continue;
    }

    if (JSON.stringify(officialSignatures) !== JSON.stringify(vendoredSignatures)) {
      const officialCounts = countValues(officialSignatures);
      const vendoredCounts = countValues(vendoredSignatures);
      for (const [signature, count] of officialCounts) {
        const missing = count - (vendoredCounts.get(signature) ?? 0);
        if (missing > 0) failures.push(`${family} missing official tuple ${signature}${countSuffix(missing)}`);
      }
      for (const [signature, count] of vendoredCounts) {
        const unexpected = count - (officialCounts.get(signature) ?? 0);
        if (unexpected > 0) failures.push(`${family} has unexpected tuple ${signature}${countSuffix(unexpected)}`);
      }
    }
  }
  return failures;
}

function elementText(xml, element) {
  const content = xml.match(new RegExp(`<${element}(?:\\s[^>]*)?>([\\s\\S]*?)<\\/${element}>`, "i"))?.[1];
  if (content === undefined) return undefined;
  return decodeXml(content.replace(/<[^>]*>/g, " ")).replace(/\s+/g, " ").trim();
}

function parseReachability(value, family, address) {
  if (value === undefined || value === "" || /^N\/A$/i.test(value)) return null;
  if (/^True$/i.test(value)) return true;
  if (/^False$/i.test(value)) return false;
  throw new Error(`IANA ${family} record ${address} has unknown Globally Reachable value ${value}`);
}

function normalizeVendoredReachability(value) {
  if (value === true || value === false || value === null) return value;
  throw new Error(`Globally Reachable must be true, false, or null; received ${String(value)}`);
}

function normalizeCidr(family, rawCidr) {
  const compact = decodeXml(String(rawCidr)).replace(/\s+/g, "").toLowerCase();
  const parts = compact.split("/");
  const expectedIpVersion = family === "ipv4" ? 4 : 6;
  const maxPrefix = family === "ipv4" ? 32 : 128;
  const prefix = Number(parts[1]);
  if (parts.length !== 2 || isIP(parts[0]) !== expectedIpVersion || !Number.isInteger(prefix)
      || prefix < 0 || prefix > maxPrefix) {
    throw new Error(`${rawCidr} is not a valid ${family} CIDR`);
  }
  return `${parts[0]}/${prefix}`;
}

function tupleSignature({ family, cidr, globallyReachable }) {
  return `${family}|${cidr}|${globallyReachable === null ? "null" : String(globallyReachable)}`;
}

function compareTuples(left, right) {
  return tupleSignature(left).localeCompare(tupleSignature(right));
}

function countValues(values) {
  const counts = new Map();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  return counts;
}

function countSuffix(count) {
  return count === 1 ? "" : ` (${count} copies)`;
}

function assertFamily(family) {
  if (!FAMILIES.includes(family)) throw new Error(`Unsupported address family ${family}`);
}

function decodeXml(value) {
  return value
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&quot;", "\"")
    .replaceAll("&apos;", "'")
    .replaceAll("&amp;", "&");
}
