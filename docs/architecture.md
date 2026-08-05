# Architecture

The canonical commercial architecture is defined in
[`2026-07-28-hermes-connector-commercial-architecture-design.md`](2026-07-28-hermes-connector-commercial-architecture-design.md).

The future enterprise AI workbench, company Skill, permission-aware knowledge,
and work-collaboration architecture is defined in
[`2026-07-28-enterprise-ai-workbench-expansion-design.md`](2026-07-28-enterprise-ai-workbench-expansion-design.md).

The future agent-native enterprise operating model, dynamic workbench, and
two-layer Hermes Agent evolution loop are defined in
[`2026-07-28-hermes-agent-native-enterprise-operating-model-design.md`](2026-07-28-hermes-agent-native-enterprise-operating-model-design.md).

The canonical governance and technology baseline for work information, files,
internal and external data, agent data exchange, AI processing, dynamic
visualization, data integration, distribution, and lifecycle propagation is
defined in
[`2026-07-29-hermes-agent-native-data-governance-and-ai-data-platform-design.md`](2026-07-29-hermes-agent-native-data-governance-and-ai-data-platform-design.md).

The owner-level operating philosophy, dual ILPO, external growth, internal
capability, and organizational AI theory are defined in
[`2026-07-29-hermes-ai-native-enterprise-owner-operating-philosophy.md`](2026-07-29-hermes-ai-native-enterprise-owner-operating-philosophy.md).

The board- and CEO-facing investment case, evidence boundaries, value
realization gates, ROI/TCO method, and low-maintenance operating model are
defined in
[`2026-07-29-hermes-ai-native-enterprise-investment-business-case.md`](2026-07-29-hermes-ai-native-enterprise-investment-business-case.md).

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

Alibaba Cloud is the initial provider for the Chinese-mainland deployment. The
current single-node closure uses SQLite through the SQLAlchemy ORM; direct SQL
and database access outside ORM adapters are forbidden. WAF, ALB, ACK Pro,
ApsaraDB RDS for PostgreSQL, OSS, KMS, and provider observability remain the
later commercial scale-out mapping. Product-specific integrations stay behind
infrastructure adapters and do not enter the Connector Protocol.

The Connector is an independent Python service with its own environment and
release lifecycle. It does not import Agent internals, read SessionDB, expose the
local Dashboard, connect directly to NATS, Redis, or PostgreSQL, or call the
Hermes API Server. All local Agent traffic crosses the Agent Local Gateway
published by the Plugin.

## Cloud Agent identity and pairing

`agent_id` is the Cloud-wide selectable identity and routing key for an Agent.
Selecting Agent A or Agent B changes data (`agent_id` and its authorized
binding); it does not select a different API, service implementation, code path,
or Android build. Production source must not special-case test Agent names.

Pairing binds one Connector device credential to an authorized
`workspace_id + agent_id` route. It proves device possession and Cloud
authorization only. It does not prove that a local Hermes runtime is running,
that the Plugin has registered, that the Local Gateway handshake succeeded, or
that Observer/Control is available. Those conditions require a separate live
runtime descriptor and capability handshake.

## Client direction

H5/PWA is the first-closure client in the current delivery phase; Android work is
paused. H5 consumes only the public Cloud REST/realtime contracts and never joins
the Connector or Local Gateway protocols. Resuming Android later does not change
that public boundary or create a client-specific Agent path.

## Session consistency

Durable session keys, runtime session IDs, runtime generations, lineage tips,
client turn IDs, event sequence numbers, command IDs, and controller lease
revisions are distinct values. No client may infer one from another.

The durable Cloud projection identity is
`tenant_id + agent_id + profile + session_key`; `session_id` is its stable
opaque row identity and is the only session reference carried by a bound
WebSocket ticket. Reads that omit `profile` must resolve exactly one authorized
profile or fail closed.

## Local read protocols

Hermes 0.19 still lacks the public Host SPI required by live Observer and
Control. Its standard API Server `GET` session/message endpoints also omit the
runtime generation, runtime session identity, live running/status authority,
and per-runtime event sequence required by those protocols. They therefore
must not be mapped to an Observer snapshot/event, used to advertise Control, or
used to move the Connector into an Agent-ready state.

A bounded, read-only catalog/history synchronization protocol is allowed as a
separate future capability. Its Hermes HTTP adapter belongs inside the Plugin;
the Connector may receive only normalized catalog records through the Local
Gateway. Catalog records are non-authoritative read projections: they carry
explicit source/provenance and cursor metadata, never synthesize live runtime
fields, never create a controller lease, and never imply runtime connectivity.
The Connector must not hold the API Server endpoint or bearer key. Until this
separate contract, field policy, credential handling, and tests exist, the
capability remains unavailable.

## Current delivery evidence

The local Cloud candidate has completed revision 11: four-part durable session
identity, stable `session_id` WebSocket tickets, profile isolation, typed ORM
SQLite migration, and a PostgreSQL v10 non-empty-source fail-closed gate. The
recorded final local gates are Cloud `1513 passed`, root contract suites
`101 passed` and `67 subtests passed`, and H5 `327` application, `6` IANA,
`6` bundle-budget, and `15` process-lifecycle tests. H5 typecheck, lint, and
production build also pass.

Database behavior evidence is intentionally asymmetric for this SQLite
milestone: SQLite executes the shared mapped ORM predicate in real temporary
transactions. PostgreSQL coverage verifies the same mapped predicate's dialect
structure and compilation only; a future PostgreSQL deployment must pass a
real PostgreSQL behavior suite before claiming transactional parity.

The current H5 backend increment adds canonical cookie-authenticated
`GET /api/v1/agents` and `GET /api/v1/agents/{agent_id}/sessions` routes while
retaining the prior unversioned routes as compatibility aliases. Logout revokes
the refresh authority before clearing cookies, so pre-logout access credentials
and WebSocket tickets fail closed. A local integration gate executes the current
browser auth/catalog clients through the real Vite proxy against a real Cloud
ASGI process and temporary SQLite ORM database. The integration rows named
`integration-agent` and `Integration session` are test fixtures; this evidence
does not cover Connector, Plugin, the local Hermes runtime, or remote deployment.

The canonical directory routes reject unknown or repeated query parameters;
the canonical Agent-scoped session route also rejects query-form `agent_id`.
The unversioned `/api/agents` and `/api/sessions` operations remain explicitly
deprecated in the published OpenAPI document so compatibility behavior cannot
silently diverge from the public contract.

This candidate has not been deployed remotely. The last verified remote is the
older SQLite release and uses deterministic test-server seed rows such as
`android-agent`, `android-bootstrap`, and `Hermes Cloud test session`. Those
rows are not an Android Agent or real Hermes session output. Earlier public
Android, pairing, and Observer screenshots are retained only as dated
test-server evidence.

## Security

Connector device keys stay in the operating system secure store. Every control
command is scoped to tenant, device, Agent, session, runtime generation, client
instance, and control lease. Model/provider credentials, sudo passwords,
secrets, and terminal-sensitive input never leave the Hermes execution host in
Cloud-readable plaintext.

All Cloud and Connector business reads, writes, and migrations use typed ORM or
typed migration operations. The only direct database statements allowed are
the fixed, centrally tested SQLite connection-policy PRAGMAs; that exception
does not extend to repositories, application services, migrations, seed tools,
or deployment scripts.

## Legacy tunnel

WireGuard, SSH reverse tunnels, and direct public Dashboard exposure are not
part of the approved product path. H5 connects to Cloud, and Cloud reaches the
independent Connector over its authenticated outbound WSS channel only.
