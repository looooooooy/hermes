# Hermes Mobile Control Contract v1

This contract is the frozen cross-repository boundary for Hermes Mobile control work. It preserves the existing observer contract and owner transport.

## Invariants

- The Mac Hermes runtime and SessionDB remain authoritative.
- Observer and control are immutable, separate WebSocket roles.
- Observer allowlist remains subscribe/unsubscribe only.
- At most one explicit remote controller lease exists per live runtime.
- Mobile mutations never call `session.resume`/`session.activate` and never replace `session["transport"]`.
- Unknown command status is reconciled, never automatically resent.
- Tickets, lease IDs, secrets, full approval payloads, and default full tool output are never logged.

## Ticket compatibility

Existing endpoint:

```text
POST /api/auth/ws-ticket
```

Legacy request `{}` must keep working and mints a legacy unscoped ticket. A
native observer sends the exact client-scoped shape without a session target:

```json
{
  "connection_role": "observer",
  "client_instance_id": "uuid"
}
```

A control client sends the exact session-bound shape:

```json
{
  "connection_role": "control",
  "client_instance_id": "uuid",
  "session_key": "durable-lineage-root",
  "profile": "default"
}
```

Browser clients authenticate this ticket endpoint with the existing
`hermes_session_at` Secure HttpOnly SameSite=Strict cookie. A cookie-only mint
is accepted only when the request is HTTPS and its `Origin` exactly matches the
effective request host and port. Reverse-proxy forwarding is authoritative only
from the configured loopback proxy; arbitrary client-supplied forwarded headers
are ignored. Native Bearer authentication remains supported. If both credentials
are present they must independently authenticate to the same principal and
provider, otherwise the request fails closed. Password login and ticket responses
never return an access or refresh token in the JSON body.

The ticket is short-lived and single-use. Its in-memory claims bind authenticated principal/provider, immutable role, client instance, and mint time. Observer tickets may omit session/profile; control tickets require both durable `session_key` and `profile`, and every control RPC may only repeat those immutable target claims for consistency checking. The response echoes `connection_role`; legacy response compatibility keeps `ticket` and `ttl_seconds` unchanged.

`gateway.ready` continues to advertise `observer_contract: 1`; a control socket additionally advertises `control_contract: 1` and `connection_role: "control"`.
With a live owner route it advertises exactly the ten control and safe-mutation
methods documented below. Error `4306` is a definitive deadline failure before
the owner effect and must not trigger status lookup or automatic resend. Error
`4307` means the owner effect is unknown; the client queries
`session.command.status` with the original mutation `method` and
`client_request_id`, and never automatically resends the mutation. The status
method is required and is exactly one of `prompt.submit`, `session.interrupt`,
`session.steer`, `approval.respond`, or `clarify.respond`.

## Control RPCs

```text
session.control.acquire
session.control.renew
session.control.release
session.control.status
session.command.status
```

Acquire parameters:

```json
{
  "session_key": "durable-lineage-root",
  "profile": "default",
  "runtime_session_id": "optional-current-runtime-id",
  "client_instance_id": "uuid"
}
```

Acquire/renew result:

```json
{
  "lease_id": "opaque-never-logged",
  "expires_at_epoch_ms": 0,
  "control_revision": 1,
  "controller_kind": "mobile",
  "controller_label": "Hermes Mobile",
  "pending_input": null
}
```

Status never returns reusable lease material:

```json
{
  "controller_kind": "desktop | mobile | none",
  "controller_label": "display-safe label",
  "control_revision": 1,
  "lease_expires_at_epoch_ms": 0,
  "pending_input": null
}
```

The canonical wire values for `controller_kind` are exactly `desktop`, `mobile`,
and `none`. A legacy owner adapter may report `local`; clients normalize that
value to `desktop` at ingress and never retain `local` as a fourth controller
kind. `controller_label` is `null` when the kind is `none`; a non-empty,
display-safe label is required for an active `desktop` or `mobile` owner.

Release returns `released` and the new `control_revision`. A bounded same-principal/client reconnect grace may mint a new lease ID; possession of an old lease ID alone never authorizes another connection.

## Mutations

Frozen v1 method set:

```text
prompt.submit
session.interrupt
session.steer
session.redirect
approval.respond
clarify.respond
sudo.respond
secret.respond
terminal.read.respond
```

The frozen set reserves method names and wire boundaries; it does not make every method executable. The current lease-authorized safe subset is:

```text
prompt.submit
session.interrupt
session.steer
approval.respond
clarify.respond
```

`session.redirect`, `sudo.respond`, `secret.respond`, and `terminal.read.respond` stay fail-closed with error 4209 until each has immutable transport binding, exact lease authorization, bounded idempotency, an owner-runtime adapter, relay support, and focused contract tests. A control socket must never fall through to a raw desktop/session handler for a frozen-but-unavailable method.

Every mutation carries `session_key`, optional `runtime_session_id`, `lease_id`, and `client_request_id`. `prompt.submit` also carries `client_turn_id` and `text`. Its response returns typed `accepted | queued | rejected`, `client_request_id`, `client_turn_id`, and `server_turn_id` when allocated.

`session.steer` carries `text` but no `client_turn_id`:

```json
{
  "session_key": "durable-lineage-root",
  "runtime_session_id": "current-runtime-id",
  "lease_id": "opaque-never-logged",
  "client_request_id": "client-generated-id",
  "text": "supplemental instruction for the current execution"
}
```

Steer injects supplemental guidance into the currently running owner agent. It does not create a user turn, does not enter the busy-input prompt queue, and does not interrupt or restart execution. Mobile exposes it only while authoritative realtime state says the turn is running and an exact controller lease is held. `accepted | queued` means the guidance was accepted for the current execution; it does not mean a new turn was created or completed. `rejected` preserves the independent guidance draft.

The four running-time actions remain distinct:

- Queue: `prompt.submit` while busy; creates a later user turn and includes `client_turn_id`.
- Guide: `session.steer`; influences the current turn and has no `client_turn_id`.
- Stop: `session.interrupt`; terminates the current execution.
- Redirect: reserved stronger current-direction change; unavailable in Mobile v1 and returns 4209.

Mobile v1 does not expose a generic slash-command palette. The authoritative Desktop dispatcher resolves commands through local actions, dedicated RPCs, or `slash.exec`; `prompt.submit` is only the raw user-prompt owner adapter and must not be used to impersonate that dispatcher. Until slash completion and execution receive their own scoped, lease-authorized Mobile Control contract, the Mobile command surface consists only of the native Queue, Guide, and Stop actions above. Unknown or Desktop-only slash commands remain ordinary editable prompt text and are never auto-executed by Mobile.

Idempotency key:

```text
(session root, authenticated principal, method, client_request_id)
```

- same ID + same canonical payload → prior result;
- same ID + different payload → error 4207;
- bounded TTL/LRU ledger only;
- command status is keyed by `(method, client_request_id)` and may be queried by the same authenticated principal/client/session without a live lease;
- a resolved query returns only the generic status projection (`accepted | queued | rejected`, `client_request_id`, and optional turn identifiers), never the original approval or clarification payload;
- an unresolved query returns error `4210 command_unknown`; repeated queries remain `4210` and never authorize auto-resend;
- `4306` is definitive before-effect failure: do not query command status and do not auto-resend;
- `4307` is effect-unknown: query the original `(method, client_request_id)`, accept only the generic command-status shape, and do not auto-resend when the result remains unknown.

## Controller/pending events

Observer-safe event:

```text
session.control.changed
```

It contains only `session_key`, `runtime_session_id`, `controller_kind`, display-safe `controller_label`, `state`, and monotonic `control_revision`. No principal, ticket, lease ID, pending payload, command, or secret.

Control-only events/snapshot:

```text
session.control.state
session.command.updated
```

Every control snapshot/event carries monotonic `control_revision`. Lower/equal revisions are stale; gaps require `session.control.status` replacement.

Pending approval shape:

```json
{
  "request_id": "server-request-id",
  "kind": "approval",
  "title": "display-safe title",
  "description": "server-redacted description",
  "command": "server-redacted review text",
  "choices": ["allow_once", "allow_session", "allow_always", "deny"],
  "expires_at_epoch_ms": 0
}
```

Only server-provided choices are rendered. `allow_always` requires explicit confirmation.

Pending clarify shape:

```json
{
  "request_id": "server-request-id",
  "kind": "clarify",
  "question": "question",
  "choices": [{"id": "choice-id", "label": "choice label"}],
  "allow_other": true,
  "expires_at_epoch_ms": 0
}
```

Approval/clarify responses bind both `request_id` and `client_request_id`; only one response wins. Resolved/expired requests return a typed conflict and are removed by the next control snapshot/revision.

The controller snapshot exposes at most one actionable request: the oldest still-pending
authoritative queue entry. Resolving it reveals the next entry on the next control revision.
The queue entry owns `request_id`; display/event code must not synthesize or replace it.

`approval.respond` carries the common mutation scope plus:

```json
{
  "request_id": "server-request-id",
  "choice": "allow_once | allow_session | allow_always | deny"
}
```

The server accepts only a choice present in that pending snapshot. The control adapter maps
those wire values to the owner approval engine's internal values; clients never send owner
implementation values directly.

`clarify.respond` carries the common mutation scope and exactly one answer form:

```json
{
  "request_id": "server-request-id",
  "choice_id": "server-choice-id"
}
```

or, only when `allow_other` is true:

```json
{
  "request_id": "server-request-id",
  "other_text": "non-blank free-text answer"
}
```

Choice IDs are opaque and resolve to the server-authoritative label stored on the exact queue
entry. A successful response returns:

```json
{
  "status": "accepted",
  "kind": "approval | clarify",
  "request_id": "server-request-id",
  "client_request_id": "client-request-id",
  "control_revision": 2
}
```

The normal bounded command ledger applies. Repeating the same `client_request_id` with the
same canonical response returns the prior accepted result even after the queue entry is gone;
reusing it with different content returns 4207. A different request ID answering an expired,
already-resolved, or superseded entry returns 4208. A choice or answer form not authorized by
the exact pending snapshot returns 4213.

## Reserved error range

Mechanical enumeration of current Gateway sources on 2026-07-27 found `4200–4219` unused. v1 owns the whole range:

| Code | Meaning |
|---:|---|
| 4200 | control role required |
| 4201 | control contract unsupported |
| 4202 | authoritative live runtime unavailable |
| 4203 | another explicit controller holds the lease |
| 4204 | controller lease required |
| 4205 | controller lease expired |
| 4206 | controller lease/principal/client/transport mismatch |
| 4207 | client request ID reused with different payload |
| 4208 | pending request expired, resolved, or superseded |
| 4209 | method not allowed for connection role/slice |
| 4210 | command status unknown |
| 4211 | control revision conflict |
| 4212 | session/runtime/profile binding mismatch |
| 4213 | invalid pending-input response |
| 4214 | owner action adapter unavailable |
| 4215 | relay overloaded |
| 4216–4219 | reserved for v1; do not use without contract update |

A server collision test must mechanically scan registered/emitted errors and fail if another feature uses this range.

## Reconnect order

1. Refresh authoritative REST transcript tail.
2. Re-establish observer ticket/socket and replay/snapshot.
3. Establish control ticket/socket.
4. Call `session.control.status`; same principal/client may re-acquire within grace.
5. Reconcile outstanding command IDs.
6. Never auto-resend an unknown prompt.

## Repository ownership

- Hermes server lane owns new control modules/tests and narrow gateway/ticket integration.
- Android protocol lane owns `hermes-android/core/protocol` control types/client/tests and new pure control/command reducers.
- Timeline/UI lane owns projector/leaf transcript components/tests.
- Parent integrator exclusively owns `MainActivity.kt`, `SessionBrowserViewModel.kt`, `SessionBrowserScreen.kt`, build/manifest/resources, and cross-lane wiring.
