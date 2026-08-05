# Connector Session Protocol v1

## Authority and scope

This protocol defines the platform-neutral session between a Connector and
Hermes Cloud. Android, Web, desktop, and future clients are not participants in
this wire contract. They consume higher-level data through adapters and cannot
add fields or reinterpret sequence, capability, resume, or effect semantics.

The Cloud Envelope identifies the message type. Its payload is validated
against the schema bound in `message-types-v1.json`. A reserved message type
has no executable payload contract and must not trigger persistence, routing,
rendering, authorization, or another business effect.

## State machine

```text
DISCONNECTED --> CONNECTING --> NEGOTIATING --> ACTIVE --> DRAINING
      ^              |              |            |            |
      |              |              |            +--> RECONCILING
      |              |              |                       |
      +--------------+--------------+-----------------------+

Allowed transitions:
DISCONNECTED --> CONNECTING
CONNECTING --> NEGOTIATING, DISCONNECTED
NEGOTIATING --> ACTIVE, RECONCILING, DISCONNECTED
ACTIVE --> RECONCILING, DRAINING, DISCONNECTED
RECONCILING --> ACTIVE, DRAINING, DISCONNECTED
DRAINING --> DISCONNECTED
```

`connector.hello` proposes required and optional capabilities and supplies the
next expected sequence in each direction. The two capability sets are
disjoint. A missing required capability rejects negotiation. A missing
optional capability appears in `unavailable_optional_capabilities`; it never
causes a schema fork.

`connector.welcome` is authoritative for the connection identifier, heartbeat
interval, in-flight window, accepted capabilities, and resume decision.
`reset_required` must not trigger a command, retry a business effect, or treat
transport state as business success. Both sides enter reconciliation and use
their durable Inbox, Outbox, and cursor facts to establish a safe sequence.
`reset_required` means an authoritative rewind within the same epoch and
returns Cloud's durable cursor pair, including sequence 0. When that pair is
behind the Connector checkpoint, reconciliation replays every durable frame
through the checkpoint, whether pending, ACKed, or NACKed. Cloud persists a
same-epoch, monotonic transport recovery floor in its ORM cursor authority.
A reset target below that already-confirmed floor is invalid and fails closed;
the Connector replays the exact stored frames in
`[transport_recovery_floor, connector_checkpoint)` and may compact only
settled journal rows strictly below the floor.

`fresh` means a new epoch because the Cloud authority is absent or the
Connector instance or runtime generation changed. Its authoritative pair is
`(0, 0)`. A valid initial `fresh` proposal is exactly `(0, 0)` and its
hello/welcome handshake advances the active pair to `(1, 1)`. When a resume or
non-zero fresh proposal is reset into a new epoch, that old-epoch handshake is
not counted; the first active frame starts at sequence 0. A new epoch must not
replay settled frames from the prior epoch.

The welcome envelope sequence is the pre-handshake inbound proposal. Its
payload reports the post-handshake active pair. An exact `resumed` handshake
therefore performs a handshake-only, bidirectional `N -> N + 1` CAS without a
business-journal row at sequence `N`; a preserved `reset_required` or
old-epoch `fresh` handshake does not consume either sequence.

`connector.heartbeat` maintains liveness and exchanges the next sequence each
side expects. It is not a durable ACK, authorization decision, audit record, or
proof that a command was executed. After identity, connection, envelope
sequence, and Cloud ORM cursor-authority validation, its monotonic transport
cursor may advance the transport recovery floor. That transport compaction
fact never acknowledges an Observer fact or proves a command or owner-control
effect; Observer settlement still requires an exact `stream.ack` or
`stream.nack`.

## Authoritative session catalog lane

`session.catalog.v1` is negotiated independently from observation and control.
The Plugin exposes its Host SPI catalog over the persistent Observer-role UDS;
the catalog does not use the one-request Local Gateway handshake. The Plugin
registers the Host listener before reading page zero, buffers ordered events
while pages are staged, publishes the snapshot only after the final page, and
then drains the contiguous buffer before entering `LIVE`. A stale cursor,
changed page revision, event gap, buffer overflow, transport replacement or
runtime-generation change closes the subscription and requires a new full
snapshot.

Connector-to-Cloud snapshot pages and events omit `agent_id`, `tenant_id` and
`device_id`. Cloud resolves the writer from the authenticated pairing and
fences one current writer per Agent. Pairing authorizes that Cloud identity; it
does not prove a local Hermes runtime binding. Completing a new-generation
snapshot atomically retires all older generations for the authenticated Agent
and profile. A late generation, stale writer, page gap, event gap or revision
conflict fails closed and requests full-snapshot recovery.

Cloud sends `session.catalog.ack` only after the catalog ORM transaction has
committed. The ACK binds the original message UUID, canonical payload digest,
Connector sequence and exact terminal snapshot page (`page_index`, `is_last:
true`) or event sequence. Each NACK reason has one exact, non-conflicting
position tuple. Neither a
transport ACK nor `connector.heartbeat` settles the catalog outbox.

For each authenticated `(agent_id, profile, Host session_key)`, Cloud generates
and ORM-persists one stable RFC 4122 `session_id`. Public `id` is that stable
UUID; `_lineage_root_id` is the exact Host session key used to resolve tickets
and commands inside Cloud. Public REST detail routes, ticket requests,
Observer v2 subscribe/results/events and control requests carry only the stable
`session_id`; Host `session_key` and runtime session identifiers do not cross
those public boundaries. Catalog-only public rows carry no fabricated transcript fields:
title, start time and last activity may be null, message count is zero, and
`transcript_available` is false. Missing catalog capability makes the directory
unavailable without inventing fallback sessions.

## Adapter rule

An endpoint with fewer capabilities implements a renderer, transport, or
feature adapter and advertises only what it can perform. If a required
capability is unavailable, negotiation fails before effect. The endpoint does
not request a change to this core contract.
