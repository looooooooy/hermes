# Hermes Cross-Process Contracts

This directory is the authoritative, versioned contract packet shared by the
Agent Plugin, Connector, Cloud, Enterprise Data Gateway, and user interfaces.
Runtime projects may ship generated or compatibility copies, but must not
silently redefine these fields.

The initial packet freezes the common capability, Local Gateway discovery,
transport, handshake and error response, Connector-to-Cloud envelope, and
Connector session payloads. `LOCAL_GATEWAY_TRANSPORT_V1.md` defines the
normative local UDS lifecycle and `local-gateway-transport-v1.json` is its
machine-readable profile.
The authoritative binding between cloud `message_type` and payload schema is
`message-types-v1.json`. Entries marked `reserved` have no executable payload
contract and fail closed until their corresponding vertical slice is
implemented.

The first external Cloud client closure is frozen separately from the
Connector wire contract:

- `openapi/cloud-api-v1.json` is the REST authority for public status,
  password authentication, refresh, session projections and single-use
  WebSocket tickets. The legacy empty request mints an observer ticket; an
  exact `observer` role plus client-instance request mints a scoped observer
  ticket without a session target; an exact four-field request mints a
  session-bound control ticket. Its default
  reverse-proxy base path is `/hermes`; clients and deployments may replace
  the `basePath` server variable without changing operation paths.
  H5 directory clients prefer `GET /api/v1/agents` and
  `GET /api/v1/agents/{agent_id}/sessions`. The unversioned
  `GET /api/agents` and `GET /api/sessions` operations are frozen deprecated
  compatibility paths. Directory queries reject unknown keys and repeated
  values; the canonical Agent-scoped session route does not accept an
  `agent_id` query parameter.
- `cloud-realtime-v1.json` is the client-facing WebSocket authority for the
  `hermes.tui.v1` observer and control compatibility surface. The first server
  frame is role-aware `gateway.ready`. Observer N-1 remains unchanged. Until
  live control routing is available, control advertises no methods but still
  advertises the exact canonical control error catalog `4200–4215` plus
  deadline uncertainty errors `4306` and `4307`.
- `observer-output-parity-v2.json` and `cloud-realtime-v2.json` add the optional
  `session.observe.output-parity.v1` projection. The same ticket and WebSocket
  endpoints remain in use: an omitted `observer_contract` selects exact v1,
  while explicit `observer_contract: 2` is bound into the single-use ticket and
  echoed by ready, subscribe, snapshot/replay and live frames. Unsupported or
  mismatched v2 fails closed; it never falls back silently.
- Internal v2 observation uses the versioned `session.observe.open.v2`,
  `session.observe.close.v2`, `session.snapshot.v2`, `session.event.v2`,
  `stream.ack.v2` and `stream.nack.v2` message types. Snapshot v2 atomically
  carries current `todo_sections`, `subagents`, `tools` and `terminals`; its
  replay entries are complete v2 session events and remain globally contiguous.
- The new non-mergeable lifecycle events are `todo.update`, `subagent.update`,
  `tool.update` and `terminal.update`. Existing `tool.output.delta` and
  `agent.terminal.output` retain display-safe incremental output. Lifecycle
  metadata never carries raw arguments, raw output, reasoning, credentials,
  token values or full approval payloads.
- These client contracts are adapters. They cannot expose or reinterpret
  Connector session messages, Local Gateway messages or local control secrets.
- Password is the only P0 authentication flow advertised by status. Native
  authorization remains outside the P0 authority.

The authoritative session directory is a separate capability-gated slice:

- `session-catalog-v1.json` freezes the persistent Observer-role local RPC,
  listener-first snapshot/event race handling, bounded buffering, atomic Cloud
  replacement, generation rollover, writer fencing and full-snapshot recovery.
  It does not reuse the one-shot Local Gateway handshake.
- `schemas/conformance/session-catalog-semantic-vector-v1.schema.json` validates
  every semantic vector before execution. Malformed state or operations fail
  closed as `invalid_vector`; each snapshot operation commits atomically only
  when that operation has no errors.
- `session.catalog.snapshot.page` and `session.catalog.event` flow from the
  Connector to Cloud. `session.catalog.ack` and `session.catalog.nack` are
  durable business responses bound to the original message UUID, digest,
  Connector sequence and an exact terminal snapshot page or event position.
  Each NACK reason has one exact position tuple. A transport ACK or heartbeat is
  never a catalog commit acknowledgement; Cloud emits the business ACK only
  after its ORM transaction commits.
- Public session `id` is a stable RFC 4122 UUID generated and ORM-persisted by
  Cloud for the authenticated `(agent_id, profile, Host session_key)` tuple.
  `_lineage_root_id` carries the exact Host session key. A catalog-only row has
  `message_count: 0`, nullable transcript-derived fields and
  `transcript_available: false`; no title, time, transcript or Host identity is
  synthesized. Pairing authorizes Cloud identity but never proves that a local
  Hermes runtime is bound.
- Public REST detail routes, ticket requests, Observer v2 frames and control
  requests use only the stable UUID `session_id`. Cloud resolves that UUID to
  its ORM-owned Host lineage key; `session_key` and runtime session identifiers
  remain internal.
- An N-1 peer or a peer without `session.catalog.v1` reports the directory as
  unavailable. It must not fall back to synthetic sessions.

Device pairing is an additional Cloud REST slice:

- `device-pairing-v1.json` freezes the tenant-neutral offer, authenticated
  owner binding, five-minute code/offer-secret split, Ed25519 activation,
  repeated device challenge/token exchange, and revocation semantics.
- `schemas/cloud/device-pairing-v1.schema.json` validates that profile;
  `spec/spec-schema-device-pairing-v1.md` is its normative readable form.
- The human pairing code can only locate an offer for an authenticated owner.
  Owner claim resolves the code digest through `POST /api/device-pairing/claims`;
  the owner never needs the Connector-only offer UUID.
  Connector poll and proof require the independent high-entropy offer secret,
  and activation additionally requires the OS-secured Ed25519 private key.
- Pairing grants no control lease. `session.control.request` remains subject to
  the one-controller rule and all existing Server/Connector/Plugin checks.

Limits shared by v1 transports:

- ordinary REST JSON response: 262,144 bytes;
- REST session transcript response: 4,194,304 bytes;
- WebSocket text frame: 262,144 bytes;
- one JSON string: 131,072 UTF-8 bytes;
- nesting depth: 32, counting the root JSON object as depth 1;
- one object: 1,024 fields;
- one array: 1,024 items.

External fixture policy:

- every external profile registers valid and invalid fixtures in
  `fixtures/manifest.json`;
- N-1 compatibility and capability-degradation examples remain separate from
  ordinary valid and invalid cases so release checks can report their purpose.
  Every registered path exists; N-1 and degradation fixtures must themselves
  pass the target schema and semantic validator. When the observer wire format
  has no capability fields, `schemas/external-degradation-profile-v1.schema.json`
  records the client-neutral version, capability and safe-effect classification
  without adding synthetic fields to a wire frame;
- external schemas and semantic validators both fail closed on unknown event
  types, undeclared fields, internal credentials, discontinuous replay and
  transport-limit violations.
- Legacy Observer v1 events retain their frozen N-1 identity fields. Public
  Observer v2 live and replay events carry only stable `session_id` plus
  `event_sequence`.
  Replay ranges cannot start after their ending sequence. Every live or replayed
  `status.update` carries both `status` and `running`, and `running` is true
  exactly for the declared running statuses.
- REST semantic validation applies the shared UTF-8 string, object, array and
  nesting limits recursively, including open JSON fields such as transcript
  `content`; object keys count as strings. Endpoint-level 262,144-byte and
  4,194,304-byte total response limits remain independently enforced.
- Observer realtime RPC errors are limited to `rpc_error_codes`; control-slice
  RPC errors are limited to `control_error_codes`. Adding an advertised error
  requires first declaring and testing it in the authority.

Observer v2 contract publication is not proof of a live Hermes Host source.
The production capability must remain unavailable until the installed Host SPI
can provide authoritative, redacted lifecycle snapshots and ordered events.

## Post-deployment Android consumer gate

After the updated authority is deployed, an Android Agent must close the
following consumer drift before Android release approval. These are consumer
tasks and must not be addressed by weakening `/contracts`:

1. Observer role: require `connection_role == "observer"` in ticket responses
   and ticket minting, and reject `gateway.ready` when the role is missing or
   not `observer`.
2. Realtime fail-closed behavior: enforce the exact v2 event allowlist and required
   stable `session_id`, `event_sequence` and `payload`; reject Host
   `session_key`, runtime session identifiers, forbidden
   secret-like fields and never advance a cursor for an unknown or invalid
   event.
3. Observer RPC semantics: require the frozen empty unsubscribe result, accept
   only declared RPC error codes, align subscribe required fields with the
   authority, and enforce the bidirectional `running`/`status` rule.
4. Transport limits: count UTF-8 bytes rather than UTF-16 characters, enforce
   the 262,144-byte frame limit, decode exactly one JSON document per WebSocket
   frame without newline splitting, and require live-event `session_id`.

Run the packet checks with:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```
