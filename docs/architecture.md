# Architecture

The canonical commercial architecture is defined in
[`2026-07-28-hermes-connector-commercial-architecture-design.md`](2026-07-28-hermes-connector-commercial-architecture-design.md).

The future enterprise AI workbench, company Skill, permission-aware knowledge,
and work-collaboration architecture is defined in
[`2026-07-28-enterprise-ai-workbench-expansion-design.md`](2026-07-28-enterprise-ai-workbench-expansion-design.md).

The future agent-native enterprise operating model, dynamic workbench, and
two-layer Hermes Agent evolution loop are defined in
[`2026-07-28-hermes-agent-native-enterprise-operating-model-design.md`](2026-07-28-hermes-agent-native-enterprise-operating-model-design.md).

## Deployment boundary

```text
H5 / PWA
  -> HTTPS / WSS
Hermes Remote Server
  -> Hermes WSS Gateway
  -> NATS Core / JetStream
  -> PostgreSQL / Object Storage / KMS
  -> outbound WSS / TLS 443
Hermes Connector
  -> Unix Domain Socket / Windows Named Pipe
Hermes Agent Local Gateway
  -> Hermes Agent
```

The Remote Server is the public ingress, command fact store, device control plane,
and encrypted read-projection host. It is not a second Agent and does not own the
authoritative session database or model credentials.

Alibaba Cloud is the initial provider for the Chinese-mainland commercial
deployment. The current mapping uses WAF 3.0, ALB, ACK Pro, ApsaraDB RDS for
PostgreSQL, OSS, KMS, and Alibaba Cloud observability services. Product-specific
integrations remain behind infrastructure adapters and do not enter the
Connector Protocol.

The Connector is an independent Python service with its own environment and
release lifecycle. It does not import Agent internals, read SessionDB, expose the
local Dashboard, or connect directly to NATS, Redis, or PostgreSQL.

## Client direction

H5/PWA is the primary cross-platform client. The existing Android app remains a
native reference implementation and protocol compatibility surface; it does not
define the public deployment boundary.

## Session consistency

Durable session keys, runtime session IDs, runtime generations, lineage tips,
client turn IDs, event sequence numbers, command IDs, and controller lease
revisions are distinct values. No client may infer one from another.

## Security

Connector device keys stay in the operating system secure store. Every control
command is scoped to tenant, device, Agent, session, runtime generation, client
instance, and control lease. Model/provider credentials, sudo passwords,
secrets, and terminal-sensitive input never leave the Hermes execution host in
Cloud-readable plaintext.

## Legacy tunnel

WireGuard, SSH reverse tunnels, and direct public Dashboard exposure are
transition-only mechanisms. They must be removed from the public path after the
Connector architecture passes the commercial launch gates.
