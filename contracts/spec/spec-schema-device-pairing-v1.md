---
title: Hermes Device Pairing v1 Contract
version: 1.0.0
date_created: 2026-07-31
last_updated: 2026-07-31
owner: Hermes Platform
tags: [schema, cloud-api, pairing, device-identity, ed25519]
---

# Introduction

This specification freezes the first cross-client device-pairing and repeated
Connector-authentication contract. The machine-readable authorities are
`../device-pairing-v1.json`,
`../schemas/cloud/device-pairing-v1.schema.json`, and the pairing paths in
`../openapi/cloud-api-v1.json`.

## 1. Purpose & Scope

The contract enrolls one Connector device into a scope selected by an
authenticated owner. It covers a tenant-neutral offer, owner claim and
confirmation, Connector polling, Ed25519 proof, repeated short-lived Connector
token issuance, cancellation, expiry, and device revocation.

It does not grant a realtime control lease, replace session authorization, or
allow a Connector to select a Tenant, user, Workspace, Agent, Device, or scope.

## 2. Definitions

- **Pairing offer**: A tenant-neutral, five-minute bootstrap fact created from
  a Connector public key and display metadata.
- **Pairing session**: A Tenant-scoped fact created only after an authenticated
  owner claims an offer.
- **Pairing code**: A 40-bit human-readable one-time code used only by an owner
  to locate and claim an offer.
- **Pairing offer secret**: A 256-bit Connector-only secret returned by offer
  creation and sent only in `X-Hermes-Pairing-Offer`.
- **Device credential**: The Server binding of an Ed25519 public key to one
  Device and its authorized scope.
- **Connector token**: A short-lived bearer credential issued only after a
  single-use Ed25519 challenge succeeds.

## 3. Requirements, Constraints & Guidelines

- **REQ-001**: The Connector creates a tenant-neutral pairing offer containing
  only its public key and non-authoritative display metadata.
- **REQ-002**: The authenticated owner supplies Workspace, Agent, display name,
  and allowed scopes together with the human code; the Server atomically
  resolves the code digest. Tenant and user come from the bearer principal,
  and the owner does not need or supply an offer UUID.
- **REQ-003**: Owner confirmation must compare the full canonical public-key
  fingerprint. Before confirmation, the owner view must show Connector
  `display_name`, `platform_family`, `connector_version`, Ed25519 key algorithm,
  fingerprint, and expiry. Connector metadata is untrusted display data, not an
  authorization fact.
- **REQ-004**: Device activation requires both owner confirmation and a valid
  Ed25519 signature over a Server challenge.
- **REQ-005**: Every later Connector token requires a new single-use challenge
  and Ed25519 signature.
- **REQ-006**: Connector tokens expire within 3,600 seconds and bind Tenant,
  Device, credential, Agent, and scopes.
- **REQ-007**: `session.control.request` permits asking for control; it does not
  create or bypass the one-controller lease.
- **SEC-001**: The private key never leaves the OS secure store.
- **SEC-002**: Pairing code, offer secret, challenge, signature, and Connector
  token are forbidden in logs, traces, metrics, and diagnostics.
- **SEC-003**: Pairing code, offer secret, and challenge are persisted only as
  digests. Bearer secrets are never persisted as plaintext.
- **SEC-004**: A pairing code, offer ID, or offer secret alone cannot activate
  a Device.
- **SEC-005**: A suspended or revoked Device and an expired or revoked
  credential cannot receive a challenge or token.
- **SEC-006**: Revocation closes current Connector WSS connections and blocks
  future authentication without automatic re-pairing.
- **SEC-007**: The Server signals a revoked or suspended Device only with
  WebSocket policy close code `1008` and the exact reason
  `device_authorization_revoked` or `device_authorization_suspended`. No
  lifecycle envelope is defined. Any other close is a reconnectable disconnect
  and must not change the persisted Device lifecycle.
- **CON-001**: Pairing offer TTL is exactly 300 seconds from offer creation;
  claim and confirmation do not extend it, and a challenge cannot outlive it.
  Challenge TTL is at most 60 seconds.
- **CON-002**: Failed pairing-code lookup is counted only against the
  authenticated owner principal `(tenant_id, user_id)`. Five failures in a
  rolling 300-second window block further claims by that principal until the
  window expires. Unknown or wrong codes never mutate, cancel, or block a
  `PairingOffer`.
- **CON-003**: Every mutation requires a canonical UUID `Idempotency-Key`.
- **CON-004**: Same idempotency key and request digest replay the business
  result; a different digest fails with `IDEMPOTENCY_CONFLICT`.

State model:

```text
Offer:
PENDING -> CLAIMED
   |          |
   +-> EXPIRED
   +-> CANCELLED

Tenant-scoped session:
CLAIMED -> CONFIRMED -> proof verified -> Device ACTIVE
   |           |
   +-> EXPIRED +-> EXPIRED
   +-> CANCELLED
               +-> CANCELLED

Device lifecycle:
UNPAIRED -> PENDING -> ACTIVE -> SUSPENDED -> REVOKED
```

## 4. Interfaces & Data Contracts

| Operation | Principal | Effect |
|---|---|---|
| `POST /api/device-pairing/offers` | Unpaired Connector | Creates tenant-neutral offer and returns code plus Connector-only secret |
| `GET /api/device-pairing/offers/{pairing_offer_id}` | Pairing offer secret | Polls progress; returns challenge only after owner confirmation |
| `POST /api/device-pairing/claims` | Authenticated owner | Resolves the human code through an atomic offer-digest compare-and-swap and creates the authoritative Tenant-scoped session and Device binding |
| `GET /api/device-pairing/sessions/{pairing_session_id}` | Same owner | Polls the complete authoritative owner snapshot after confirmation; unknown and non-owned sessions use the same non-enumerable `404` response |
| `POST /api/device-pairing/sessions/{pairing_session_id}/confirm` | Same owner | Confirms fingerprint and authorization |
| `POST /api/device-pairing/sessions/{pairing_session_id}/cancel` | Same owner | Cancels before activation |
| `POST /api/device-pairing/sessions/{pairing_session_id}/proof` | Pairing offer secret plus Ed25519 proof | Activates credential and issues initial short token |
| `POST /api/device-auth/challenges` | Device bootstrap identity | Creates a single-use challenge for an active credential |
| `POST /api/device-auth/tokens` | Ed25519 proof | Issues another short-lived, device-bound token |
| `POST /api/devices/{device_id}/revoke` | Authenticated owner | Revokes Device and all credentials |

For an established Connector WSS, Device lifecycle is a transport close
contract, not an application envelope. Matching requires both code `1008` and
an exact reason. A close code or reason match by itself is non-authoritative.

The public key is the unpadded base64url encoding of the raw 32-byte Ed25519
public key. The signature is the unpadded base64url encoding of the raw 64-byte
signature. The Connector signs exactly the decoded `signing_payload` bytes
after verifying the `hermes-device-auth-v1\0` domain prefix.

## 5. Acceptance Criteria

- **AC-001**: Given an unauthenticated create request, when it includes any
  Tenant, user, Workspace, Agent, Device, or scope authority field, then schema
  validation fails.
- **AC-002**: Given only a correct pairing code, when proof is absent, then the
  Device does not become active.
- **AC-002A**: Given an unauthenticated caller, when it attempts to resolve a
  pairing code, then no lookup endpoint or result is available.
- **AC-002B**: Given a successful authenticated claim, when Android presents
  the confirmation view, then Connector display metadata, Ed25519 algorithm,
  canonical fingerprint, and expiry are present.
- **AC-002C**: Given an authenticated owner submits an unknown, wrong, expired,
  cancelled, or otherwise unavailable code, then the response is the same
  `404 PAIRING_CLAIM_UNAVAILABLE` shape and contains no offer identifier, state,
  or expiry.
- **AC-002D**: Given an authenticated owner principal reaches five failed code
  lookups inside 300 seconds, further claims return
  `429 PAIRING_CLAIM_RATE_LIMITED` with a `Retry-After` value of 1 through 300
  seconds; no `PairingOffer` is changed.
- **AC-002E**: Given two claims race for the same correct code, only the atomic
  offer-digest compare-and-swap succeeds. Repeating the successful request with
  the same idempotency key and digest replays its prior business result.
- **AC-003**: Given owner confirmation and a valid offer secret, when the
  Ed25519 signature is invalid, expired, or replayed, then activation fails.
- **AC-003A**: Given the same owner polls a confirmed pairing session no more
  frequently than once per second, then each `200` returns the complete
  authoritative owner snapshot with `Cache-Control: no-store`. Polling stops
  when activation becomes `active` or `blocked`, or when the session becomes
  `expired` or `cancelled`. Unknown and non-owned session identifiers return
  the same `404 PAIRING_NOT_FOUND` shape. The snapshot's `revision` is the
  pairing-session concurrency revision used for confirmation and cancellation;
  its distinct `device_revision` is the device-lifecycle concurrency revision
  and is the only revision accepted as `RevokeDeviceRequest.expected_revision`.
- **AC-004**: Given an active Device, when it requests another token, then a
  fresh challenge and signature are required.
- **AC-005**: Given a revoked or suspended Device, when it requests a challenge
  or token, then the request fails closed.
- **AC-005A**: Given an established Connector WSS, when the Server sends policy
  close `1008` with exact reason `device_authorization_revoked` or
  `device_authorization_suspended`, then the Connector persists the matching
  lifecycle and disables reconnect. Any non-exact close only disconnects and
  remains reconnectable.
- **AC-006**: Given two users requesting control through paired clients, when a
  controller already exists, then pairing does not bypass the realtime
  one-controller rule.

## 6. Test Automation Strategy

- Validate the profile against Draft 2020-12 JSON Schema.
- Validate all pairing valid and invalid fixtures against OpenAPI components.
- Assert exact authentication subjects, TTLs, error catalog, idempotency
  headers, secret classifications, and state transitions.
- Run the complete `contracts/tests` suite after every authority change.

## 7. Rationale & Context

Separating the tenant-neutral offer from the Tenant-scoped session prevents an
untrusted Connector from becoming an authorization source. The human code
provides usability, the offer secret prevents polling and proof enumeration,
and the Ed25519 key proves possession. All three are still subordinate to the
authenticated owner and Server policy.

## 8. Dependencies & External Integrations

- **EXT-001**: OS secure store for the Connector private key.
- **EXT-002**: Cloud owner authentication and Tenant membership enforcement.
- **INF-001**: Cryptographically secure random or PRF-backed offer secrets and
  challenges.
- **INF-002**: Gateway revocation lookup on token issue and connection.

## 9. Examples & Edge Cases

- An owner enters a valid code but rejects the fingerprint: cancel with
  `fingerprint_mismatch`; no challenge is accepted.
- The proof response is lost: the same idempotency key and digest must not
  create another Device or credential, and no plaintext bearer secret may be
  stored to support replay.
- The challenge expires after owner confirmation: issue a fresh challenge only
  while the five-minute pairing session remains valid.

## 10. Validation Criteria

- `contracts.tests.test_device_pairing_v1` passes.
- The complete contracts test suite passes.
- Every schema is valid Draft 2020-12 JSON Schema.
- Every registered fixture path exists and has the declared classification.
- OpenAPI paths contain no private key field or client-authoritative Tenant.

## 11. Related Specifications / Further Reading

- `../../../hermes-agent-plugin/docs/05-security-and-data-governance.md`
- `../../../hermes-agent-plugin/docs/13-end-to-end-logical-flows.md`
- `../../../docs/plans/feature-commercial-connector-cloud-1.md`
