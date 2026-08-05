import { describe, expect, it } from "vitest";
import {
  compareIanaRegistrySnapshot,
  parseIanaRegistryXml,
} from "./iana-registry.mjs";

const IPV4_XML = registryXml("2025-10-09", [
  record("10.0.0.0/8", "False"),
  record("192.0.0.170/32, 192.0.0.171/32", "False"),
  record("192.31.196.0/24", "True"),
  record("192.88.99.0/24"),
]);
const IPV6_XML = registryXml("2025-10-10", [
  record("2001:DB8::/32", "False <xref type=\"note\" data=\"1\"/>") ,
  record("2001:10::/28"),
  record("2002::/16 <xref type=\"note\" data=\"2\"/>", "N/A"),
]);

describe("IANA special-purpose registry normalization", () => {
  it("splits address rows and preserves true, false, and null reachability", () => {
    expect(parseIanaRegistryXml("ipv4", IPV4_XML)).toEqual({
      family: "ipv4",
      version: "2025-10-09",
      sourceRecordCount: 4,
      records: [
        tuple("ipv4", "10.0.0.0/8", false),
        tuple("ipv4", "192.0.0.170/32", false),
        tuple("ipv4", "192.0.0.171/32", false),
        tuple("ipv4", "192.31.196.0/24", true),
        tuple("ipv4", "192.88.99.0/24", null),
      ],
    });
  });

  it("normalizes nested notes, IPv6 case, missing global, and N/A", () => {
    expect(parseIanaRegistryXml("ipv6", IPV6_XML)).toEqual({
      family: "ipv6",
      version: "2025-10-10",
      sourceRecordCount: 3,
      records: [
        tuple("ipv6", "2001:10::/28", null),
        tuple("ipv6", "2001:db8::/32", false),
        tuple("ipv6", "2002::/16", null),
      ],
    });
  });

  it.each([
    ["CIDR replacement", (snapshot) => { snapshot.ipv4[0].cidr = "11.0.0.0/8"; }],
    ["false mutation", (snapshot) => { snapshot.ipv4[0].globallyReachable = true; }],
    ["null mutation", (snapshot) => {
      snapshot.ipv4.find(({ cidr }) => cidr === "192.88.99.0/24").globallyReachable = false;
    }],
  ])("detects %s across the complete tuple set", (_label, mutate) => {
    const official = officialRegistries();
    const snapshot = snapshotFor(official);
    mutate(snapshot);

    expect(compareIanaRegistrySnapshot(snapshot, official)).not.toEqual([]);
  });

  it("compares IPv4 and IPv6 versions independently", () => {
    const official = officialRegistries();
    const snapshot = snapshotFor(official);
    snapshot.registryVersions.ipv6 = snapshot.registryVersions.ipv4;

    expect(compareIanaRegistrySnapshot(snapshot, official)).toContain(
      "ipv6 version is 2025-10-10, vendored 2025-10-09",
    );
  });
});

function officialRegistries() {
  return {
    ipv4: parseIanaRegistryXml("ipv4", IPV4_XML),
    ipv6: parseIanaRegistryXml("ipv6", IPV6_XML),
  };
}

function snapshotFor(official) {
  return structuredClone({
    registryVersions: {
      ipv4: official.ipv4.version,
      ipv6: official.ipv6.version,
    },
    sourceRecordCounts: {
      ipv4: official.ipv4.sourceRecordCount,
      ipv6: official.ipv6.sourceRecordCount,
    },
    ipv4: official.ipv4.records.map(({ cidr, globallyReachable }) => ({
      cidr,
      globallyReachable,
    })),
    ipv6: official.ipv6.records.map(({ cidr, globallyReachable }) => ({
      cidr,
      globallyReachable,
    })),
  });
}

function registryXml(updated, records) {
  return `<registry><updated>${updated}</updated>${records.join("")}</registry>`;
}

function record(address, global) {
  const globalElement = global === undefined ? "" : `<global>${global}</global>`;
  return `<record><address>${address}</address>${globalElement}</record>`;
}

function tuple(family, cidr, globallyReachable) {
  return { family, cidr, globallyReachable };
}
