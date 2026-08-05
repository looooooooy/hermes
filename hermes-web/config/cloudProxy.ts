import { BlockList, isIP } from "node:net";
import type { ProxyOptions } from "vite";
import ianaSpecialPurposeRegistry from "./iana-special-purpose-registry.json";

export const DEFAULT_HERMES_CLOUD_URL = "https://api.seaotter.wiki/hermes/";
export const HEALTH_PROXY_CONTEXT = String.raw`^/(?:live|ready)(?:\?|$)`;
export const AUTH_PROXY_CONTEXT = String.raw`^/auth(?:/|\?|$)`;
export const WEBSOCKET_PROXY_CONTEXT = String.raw`^/api/ws(?:\?|$)`;
export const API_PROXY_CONTEXT = String.raw`^/api(?:/|\?|$)`;

export function createHermesCloudProxy(
  environment: Readonly<Record<string, string | undefined>>,
): Record<string, ProxyOptions> {
  const cloudUrl = new URL(environment.HERMES_WEB_CLOUD_URL ?? DEFAULT_HERMES_CLOUD_URL);
  if (
    cloudUrl.protocol !== "https:"
    || cloudUrl.port === "9119"
    || !isAllowedCloudHost(cloudUrl.hostname)
  ) {
    throw new Error("HERMES_WEB_CLOUD_URL must use an HTTPS Cloud hostname or global IP literal");
  }
  const cloudPrefix = cloudUrl.pathname.replace(/\/+$/, "");
  const createProxy = (websocket: boolean): ProxyOptions => ({
    target: cloudUrl.origin,
    changeOrigin: true,
    secure: true,
    xfwd: true,
    ws: websocket,
    headers: { origin: cloudUrl.origin },
    cookieDomainRewrite: "",
    cookiePathRewrite: "/",
    rewriteWsOrigin: websocket,
    rewrite: (path) => `${cloudPrefix}${path}`,
  });
  return {
    [HEALTH_PROXY_CONTEXT]: createProxy(false),
    [AUTH_PROXY_CONTEXT]: createProxy(false),
    [WEBSOCKET_PROXY_CONTEXT]: createProxy(true),
    [API_PROXY_CONTEXT]: createProxy(false),
  };
}

function normalizeHostname(hostname: string): string {
  const unbracketed = hostname.startsWith("[") && hostname.endsWith("]")
    ? hostname.slice(1, -1)
    : hostname;
  return unbracketed.toLowerCase().replace(/\.+$/, "");
}

function isAllowedCloudHost(hostname: string): boolean {
  const normalized = normalizeHostname(hostname);
  if (normalized === "localhost" || normalized.endsWith(".localhost")) return false;
  const family = isIP(normalized);
  if (family === 0) return true;
  return isGlobalAddress(normalized, family as 4 | 6);
}

function isGlobalAddress(address: string, family: 4 | 6): boolean {
  const registryResult = findIanaSpecialPurposeResult(address, family);
  if (registryResult !== undefined) return registryResult;
  if (family === 4) return !IPV4_NON_UNICAST.check(address, "ipv4");
  return IPV6_GLOBAL_UNICAST.check(address, "ipv6");
}

interface IanaSpecialPurposeRule {
  block: BlockList;
  globallyReachable: boolean | null;
  prefix: number;
}

const IANA_SPECIAL_PURPOSE_RULES = {
  4: createIanaRules("ipv4", ianaSpecialPurposeRegistry.ipv4),
  6: createIanaRules("ipv6", ianaSpecialPurposeRegistry.ipv6),
};

const IPV4_NON_UNICAST = createBlockList("ipv4", [
  ["224.0.0.0", 4],
]);

const IPV6_GLOBAL_UNICAST = createBlockList("ipv6", [["2000::", 3]]);

function findIanaSpecialPurposeResult(address: string, family: 4 | 6): boolean | undefined {
  const familyName = family === 4 ? "ipv4" : "ipv6";
  const rule = IANA_SPECIAL_PURPOSE_RULES[family]
    .find((candidate) => candidate.block.check(address, familyName));
  return rule === undefined ? undefined : rule.globallyReachable === true;
}

function createIanaRules(
  family: "ipv4" | "ipv6",
  records: ReadonlyArray<{ cidr: string; globallyReachable: boolean | null }>,
): IanaSpecialPurposeRule[] {
  return records.map((record) => {
    const separator = record.cidr.lastIndexOf("/");
    const network = record.cidr.slice(0, separator);
    const prefix = Number(record.cidr.slice(separator + 1));
    return {
      block: createBlockList(family, [[network, prefix]]),
      globallyReachable: record.globallyReachable,
      prefix,
    };
  }).sort((left, right) => right.prefix - left.prefix);
}

function createBlockList(
  family: "ipv4" | "ipv6",
  subnets: ReadonlyArray<readonly [string, number]>,
): BlockList {
  const blockList = new BlockList();
  for (const [network, prefix] of subnets) blockList.addSubnet(network, prefix, family);
  return blockList;
}
