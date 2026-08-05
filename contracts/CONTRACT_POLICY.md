# Core Contract Authority and Consumer Adaptation

## Authority

`/contracts` is the only normative source for cross-process Hermes data
exchange. Agent Plugin, Connector, Cloud, Android, Web, desktop clients, and
future clients are consumers. A consumer copy, generated type, test fixture,
renderer, or transport implementation cannot redefine the core contract.

The dependency direction is one way:

```text
Core Contract
    |
    +--> Plugin adapter
    +--> Connector adapter
    +--> Cloud adapter
    +--> Android adapter
    +--> Web adapter
```

No arrow points back to the Core Contract.

## Capability negotiation

- A consumer advertises only capabilities it actually implements.
- Missing optional capability causes a defined degradation, not a schema fork.
- Missing required capability fails the handshake before any business effect.
- Required and optional capability sets must be disjoint.
- A client upgrade adds the missing adapter or renderer; it does not add
  platform-specific top-level fields to a core message.
- `RENDERED`, `DELIVERED`, and transport ACKs never imply authorization or
  business success.

## Extension rule

Core transport objects reject unknown top-level fields. Experimental or
consumer-specific metadata must be placed under `extensions` with a
reverse-domain-style namespace such as `com.example.feature`. An extension:

- cannot change the meaning of a core field;
- cannot carry credentials or bypass policy;
- cannot be required unless a negotiated capability says so;
- must be ignored safely by consumers that did not negotiate it;
- must graduate into a versioned core field if it becomes required by more
  than one independent consumer.

Names such as `android`, `web`, `ios`, or `desktop` are not core schema fields.

An operating-system transport adapter may change endpoint discovery and native
I/O primitives, but it cannot change a versioned JSON body. POSIX Local Gateway
v1 uses the framing and security rules in `LOCAL_GATEWAY_TRANSPORT_V1.md`;
Windows Named Pipe support remains an adapter boundary, not a JSON schema fork.

All JSON decoders must reject duplicate object member names before schema or
business validation. A decoder must not silently choose the first or last
duplicate value.

The root JSON object has nesting depth 1. Canonical UUID text is lowercase
hyphenated form. Protocol timestamps are UTC RFC 3339 values ending in `Z`.

## Compatibility

- Core schemas, error catalogs, fixtures, and state vocabulary are versioned.
- A breaking semantic or required-field change creates a new major contract.
- Additive optional behavior requires capability negotiation and N-1 fixtures.
- Each consumer must pass the same valid/invalid fixtures before release.
- Consumer copies are generated or synchronized from `/contracts`; CI rejects
  drift.

An Envelope-level validator does not authorize or dispatch an opaque payload.
Every `message_type` must gain its own payload schema and policy gate before it
can trigger persistence, routing, rendering, or another business effect.

## Observer output parity v2

`observer-output-parity-v2.json` is the semantic authority for the optional
`session.observe.output-parity.v1` capability. A Connector may send the
versioned `session.*.v2` and `stream.*.v2` messages only when the capability was
offered and accepted for that connection. Capability loss never changes an
active stream in place; the subscription closes and renegotiates from a fresh
authoritative snapshot.

External clients select observer contract 2 explicitly when minting the
single-use WebSocket ticket. The selected version is an immutable ticket claim
and must be echoed by `gateway.ready`, subscribe request, subscribe result and
event frames. Omitting `observer_contract` at ticket minting selects the exact
v1 observer contract. An unsupported or mismatched explicit v2 selection fails
closed without a v1 ticket or silent fallback.

Snapshot v2 atomically replaces Todo, Subagent, Tool and Terminal lifecycle
projections. Every replay item is validated by the complete
`session-event-v2.schema.json` authority before application. JSON Schema owns
exact shapes and scalar/collection limits; consumers additionally enforce the
semantic rules in the machine policy, including contiguous ranges, compound
identity uniqueness, monotonic revisions, first-occurrence bounds, Todo item
identity/order, Subagent parent closure/depth/cycles and terminal deletion.

Publishing these contracts and generated resources does not assert that the
installed Hermes Host can produce authoritative lifecycle facts. Real-source
activation remains fail closed until the Host observer SPI exposes the required
snapshot and event authority.

## UI and enterprise data

Enterprise data is exposed through governed Data Products and opaque resource
references. `view.card` is a platform-neutral presentation contract. Android
and Web may render different component subsets, but they must report their
renderer capability and use the same Card state, authorization, provenance,
freshness, and action semantics.
