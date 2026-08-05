# Hermes Connector macOS Runtime

The macOS adapters are the only production platform composition currently
available. Formal `run` consumes the frozen root Observer contracts and remains
fail-closed whenever the Cloud-authorized target, local runtime authority,
Observer endpoint, sequence continuity, or durable business acknowledgement is
invalid. Linux and Windows remain fail-closed until their platform-specific
service, discovery, transport, credential, identity, and lock adapters exist.
The CLI selects the platform before lazily importing the macOS composition, so
help and the explicit unavailable-platform result do not import macOS-only
modules.

Production composition has no Fake Host, fixture descriptor, embedded Agent, or
legacy Observer fallback. It connects only to the role-separated descriptors
published by the Plugin for the current effective user. If the running Hermes
Host lacks `gateway-extension/1`, the Plugin correctly publishes no usable
Local, Control, or Observer endpoint. Initial Local Gateway negotiation then
raises the stable machine-readable category `local_runtime_unavailable`
(`retryable=true`); it is not rewritten as a deadline and cannot authorize a
Cloud session.

## Runtime layout

The composition keeps platform responsibilities separated:

- `adapters/platform/macos/` owns the macOS Keychain, Ed25519 device identity,
  explicit migration-only credential-file validation, stable process-instance
  identities, private pairing projections, role-specific Local Gateway and
  Observer registry discovery, Unix-domain socket transport, and the process
  lock.
- `bootstrap/macos.py` composes the macOS adapters with the shared
  `SQLiteStorageComponent`, `LocalGatewayClient`, `MacOSObserverClient`,
  `ObserverIntentLane`, `ObserverOutboundLane`, `MacOSPluginControlRelay`,
  bounded pairing HTTP client, device-bound token provider, `CommandLane`,
  `CloudWSSClient`, `Supervisor`, and `ServiceRunner`.
- `cli.py` validates configuration, installs process signal handlers, and runs
  the composed service. It does not edit Hermes Agent or user configuration.

The Supervisor composition starts components in this order:

1. `sqlite_storage`
2. `keychain_broker`
3. `local_gateway`
4. `device_pairing_http`
5. `cloud_wss`

Drain and stop happen in reverse order. The role-specific
`MacOSObserverEndpointDiscovery` is configured only with the Observer registry
and socket namespaces; it does not fall back to the generic or control
namespaces. `session.observe.open` is the only source of the explicit
`profile/session_key` target. The Connector requires exactly one trusted
endpoint and never selects the first endpoint from an ambiguous set.

This is a known-session observation path, not initial session discovery. With
an empty Cloud projection, the Connector currently has no authoritative Host
SPI operation or Connector Protocol message that can enumerate the running
Hermes profile's durable sessions. It must not infer them from API Server GET,
local files, test seeds, placeholder Cloud sessions, or observer endpoint
descriptors. H5 cannot automatically show the real local Hermes session list
until Stage 3 supplies a bounded generation-bound Host session catalog and the
Plugin, Connector, and Cloud carry its revisioned changes end to end. The
catalog projection and durable delivery must remain SQLAlchemy ORM-backed.

Control descriptor discovery is bounded at the filesystem iterator: it reads
at most 65 directory entries and retains at most 32 matching descriptor names.
Exceeding either the 64-entry scan budget or the 32-candidate budget fails the
discovery attempt closed without enumerating the rest of the directory. Each candidate is opened
relative to the already verified private registry directory with
`O_NOFOLLOW`; owner, exact mode, regular-file type, size, device/inode,
modification time, and length are checked around one bounded read from that
same file descriptor. A symlink, replacement, in-place mutation, oversized
file, untrusted owner or mode, or non-regular file is ignored without deleting
or repairing user files. The socket is then checked relative to the verified
private socket directory before it can be selected. Discovery, UDS connect,
attach, lease acquisition, and mutation share one caller deadline; a timeout
through lease acquisition is a retryable before-effect failure and cannot start
a later mutation. Once sending the mutation frame begins, a send failure,
response timeout, connection loss, or malformed response is reported as
effect-unknown because a partial write may already have reached Hermes; an
exact, recognized error response remains deterministic. External task cancellation is
not converted into either result.

Generic Local Gateway and Observer discovery apply the same same-file rule as
Control discovery. Each descriptor is opened relative to its already verified
private registry directory without following links, read through that file
descriptor, then checked again with `fstat`. Device, inode, modification time,
size, owner, exact `0600` mode, and regular-file type must remain unchanged, and
the bytes read must equal the opened size. In-place mutation, replacement,
symlink, stale PID, missing socket, wrong owner, or wrong mode invalidates only
that candidate. Generic and Observer discovery also stop after 65 total directory
entries, retain at most 32 descriptor candidates, and fail the complete attempt
closed on either overflow instead of selecting a trusted-looking subset.
Discovery is read-only and never removes or repairs stale user files.

All three role registries accept only discovery descriptor version 2 with this
exact field set: `version`, `pid`, `profile`, `runtime_generation`,
`socket_path`, `instance_id`, `process_start_time_ns`, `process_executable`,
`process_executable_device`, `process_executable_inode`, and `host_bundle_id`.
`DISCOVERY_DESCRIPTOR_VERSION` and `DISCOVERY_DESCRIPTOR_FIELDS` in the Local
Gateway domain contract are the canonical Connector-side definition imported
by the Local, Control, and Observer readers.
Version 1, unknown versions, missing or extra fields, and malformed evidence are
rejected; Connector does not guess a migration. On macOS it obtains the
publisher start time and executable path/device/inode from the kernel and
requires them to equal the descriptor both before and after socket inspection.
This prevents a reused numeric PID from inheriting trust from a stale
descriptor. `instance_id`, `runtime_generation`, and `host_bundle_id` remain
descriptor-published runtime identity and are bound across roles; they are not
represented as code-signing evidence.

After each Local, Control, or Observer UDS connect, Connector reads the macOS
`LOCAL_PEERPID` credential from the connected socket and requires it to equal
the PID in that role's already verified descriptor before sending a handshake,
attach, subscribe, lease, or mutation frame. It also requires the socket
device/inode and publisher process evidence to remain unchanged after connect.
This post-connect binding closes the path-replacement and PID-reuse windows
between descriptor/socket metadata inspection and `connect`; unavailable or
mismatched peer credentials, a same-UID replacement socket, or a replaced
publisher process fail closed before any Agent effect.

The Local welcome creates one authoritative `LocalRuntimeAuthority` containing
the exact profile, runtime generation, instance ID, host bundle ID, and kernel
process evidence. Control and Observer must match that authority before opening
their role socket and must match it again immediately before attach or
subscribe. An otherwise valid endpoint published by a different Hermes runtime
therefore cannot join the active Local session.

Before formal `run` creates a Connector identity, process lock, SQLite file,
WAL, schema, or migration, a read-only Local preflight discovers a trusted
version-2 endpoint and proves its peer PID through a UDS connection that sends
zero bytes. No trusted endpoint produces the structured
`local_runtime_unavailable` result (`retryable=true`) and leaves the state tree
unchanged. The later Local handshake does not reuse preflight trust: discovery,
socket identity, process evidence, and peer credentials are checked again, and
the discovered endpoint must exactly equal the preflight endpoint.

Formal Keychain mode supervises all
five components; the one-shot migration composes only the bounded Keychain
broker and pairing HTTP client needed to obtain a device challenge and freshly
issued token. Startup is
ready only after every selected component is ready. A
local Agent negotiation failure or Cloud authentication or negotiation failure
therefore fails startup and triggers bounded cleanup.
After readiness, an unexpected component exit also completes supervision,
triggers the same bounded reverse-order cleanup, releases the process lock, and
ends the service with a safe runtime-failure category.

Agent descriptor reads use one lazily created, bounded macOS discovery worker
owned by `MacOSAgentDiscovery`; they never borrow asyncio's process-wide default
executor. `LocalGatewayClient.stop()` closes that discovery port after rejecting
new work and joins an in-flight or cancelled read through an idempotent cleanup
barrier. A connection-close failure cannot skip the discovery join or the final
socket close. Connection close attempts are single-flight per client: a waiter
rechecks the captured connection identity after acquiring the close lock, so a
successful first close is not repeated, a failed close remains retryable, and
an older close completion cannot clear a replacement connection.

The Cloud client accepts only the frozen `command.deliver` methods. It advances
the durable inbound cursor only after `CommandLane` has stored and completed the
local dispatch attempt. Receipt and result payloads remain pending in the
SQLite command ledger because Connector Protocol v1 has no explicit durable
business acknowledgement for them. Neither a successful WebSocket send nor a
heartbeat cursor clears those records. Disconnects discard only in-memory send
suppression, so the durable messages are replayed with the same command
identity after reconnect.

Observer transport is WebSocket over the trusted macOS Unix-domain socket, not
Hermes TCP. The client requires `gateway.ready`, sends
`session.observe.subscribe` with the exact Cloud-authorized `profile` and
`session_key`. Contract v1 remains an explicit legacy path. Contract v2 is
enabled only when the trusted Observer descriptor and authoritative
`local.welcome` both accept `session.observe.output-parity.v1`, after which
`gateway.ready` must declare exactly Observer contract 2. A failed v2 handshake
never downgrades to v1. Authority is checked again after ready and after the
subscribe response. Only then does Connector release queued live events and
apply the generated v2 snapshot/replay/live contract: exact profile, runtime
generation, runtime session and session identities; contiguous mergeable range
starts with the cursor advancing to the range end; stable composite entity
identity; revision-plus-one replacement with `first_event_sequence` bounded by
the current event; terminal status/core absorption with only missing or null
safe metadata enrichment; delete tombstones; stable Todo item ID, label, and
order with tail-only append; generated live collection limits; bounded
subagent trees; and turn-scoped tool and terminal output. Any disagreement
requires a new snapshot instead of an inferred repair.
Profile and session inputs are bounded and validated before discovery.
Authority lookup, dedicated descriptor discovery, UDS connect,
`gateway.ready`, subscribe request/response, post-response authority validation,
snapshot validation, and sequence-guard construction consume one monotonic
caller deadline. Completing one phase never resets the remaining budget;
timeout or cancellation closes any opened WebSocket through the bounded cleanup
barrier.

The intent lane maintains a bounded set of exact
`observer_contract/subscription_id/profile/session_key` targets. Each target has its own local
subscription, event pump, and recovery epoch, so Android and Web observers can
watch different sessions concurrently. A gap, authority replacement, or exact
NACK closes and re-snapshots only the matching target. Live forwarding for that
target remains paused until Cloud ACKs the exact replacement attempt identity,
including message ID, Connector sequence, payload digest, message type,
contract, scope, and event cursor. Every local resnapshot and NACK recovery
forces a new outbox attempt even when the snapshot fact and digest are
unchanged; other targets continue independently. Duplicate exact opens are
idempotent, the active-target bound applies backpressure before local
subscription, and
`session.observe.close` or `session.observe.close.v2` stops only the matching
Cloud subscription.

Every explicit v1 `session.snapshot`/`session.event` and v2
`session.snapshot.v2`/`session.event.v2` fact is encoded before transport and
stored through SQLAlchemy ORM in the SQLite `observer_outbox` table with its
SHA-256 payload digest, message ID, global Connector sequence, observer fact
identity, canonical payload, and exact envelope frame. Reconciliation replays
the stored frame with the same message ID and sequence. A successful WebSocket
write and every heartbeat cursor are transport facts only and never settle the
business record. Only an exact same-version `stream.ack` or `stream.ack.v2`
changes a pending fact to `acked`. An exact same-version `stream.nack` or
`stream.nack.v2` retains the rejected attempt for audit. Retry or
recovery creates a new message ID, Connector sequence, and envelope frame while
retaining the same observer fact identity; schema migration v5 removes
fact-level uniqueness and keeps indexed attempt history. Repeated delivery of
an old NACK cannot restart a completed or current recovery epoch. Digest,
identity, or sequence disagreement fails closed. Canonical observer payloads
use strict sorted UTF-8 JSON without ASCII escaping or non-finite numbers, and
the generated cross-module digest vector is exercised through the ORM outbox.
Schema migration v7 extends the ORM constraints to explicit v2 message types.
The generated v2 schemas and a recursive display-safety gate reject raw
arguments, raw tool output, private reasoning, full approval payloads, tokens,
credentials, secret-bearing values, and compound sensitive extension fields
such as client secrets, API/access keys, tool arguments/output, and private
paths before projection or persistence. Nonnegative aggregate `token_counts`
remain allowed as the contract's explicit exception.

`connector.hello` never accepts a configured runtime generation or configured
capability authority. Before reading a token or opening Cloud transport,
`CloudWSSClient` waits for the ready `LocalGatewayClient` authority snapshot
negotiated from the local Hermes `local.welcome`. The hello uses that exact
runtime generation, the required locally accepted capabilities, and only the
optional capabilities actually available locally. Loss or change of that
snapshot closes the stale Cloud session with a reconnect gate before the next
send or receive; reconnect obtains a fresh local authority and emits a new
hello. `HERMES_CONNECTOR_RUNTIME_GENERATION` and JSON `runtime_generation` are
therefore forbidden rather than treated as migration inputs.

The macOS runtime uses the authenticated device identifier as the local
Connector service principal and `hermes-cloud` as its provider. Session/runtime
ownership remains authoritative in the Plugin control relay: the Connector
validates the tenant, device, connector instance, profile, method, and TTL
before persistence, while the Plugin validates the command's session,
runtime generation, controller lease, and owner action on its protected UDS.

`FoundationNoOpLocalProjectionInvalidator` is intentionally a Foundation-only
adapter with `foundation_effect = "none"`. It stores and invalidates no business
projection state. It must be replaced by an explicit projection implementation
before projection behavior is claimed.

The production credential mode is `keychain`. Cloud access tokens are stored
as a generic-password item under a versioned Hermes Connector service name.
They are short-lived and device-bound. Before each WSS connection, a missing or
near-expiry token is renewed through the bounded device challenge and signature
endpoints. The server-issued tenant, device, credential, Agent, scopes, and
expiry are checked against the paired projection before the token is accepted.
The adapter calls macOS Security.framework directly through bounded ctypes
bindings and disables interactive Keychain prompts before every operation.
Secret bytes remain in Connector memory, a private one-shot pipe, and Keychain
only. The broker starts one fixed Python module with isolated interpreter mode
(`-I`), fixed argv, a sanitized environment, and a trusted installation
working directory. A caller-controlled current directory cannot shadow the
installed helper package. Secrets never enter a shell, argv, environment
variable, terminal, or log. Each Security.framework operation runs in that
dedicated helper process, which instantiates the direct ctypes adapter inside
the child. Requests and responses use a strict length-bounded framed binary
stdin/stdout protocol and never use pickle. Helper launch, request write and
drain, and response read share the primary five-second deadline and remain
asynchronous to the Connector event loop. Process reaping has its own short,
bounded grace window. On timeout or cancellation the helper is terminated and
reaped, with a kill-and-reap fallback, before the lock can be released or
another mutation can start. Repeated task cancellation cannot interrupt that
cleanup barrier; cancellation is restored only after the child is reaped. A
new helper then gets up to five seconds to read back or repeat the digest-bound
delete CAS. A second helper failure returns an explicit effect-unknown,
fail-closed error rather than claiming rollback.

Stored values use a transparent versioned envelope containing a random
per-write revision and the logical payload. Callers receive only the payload.
The revision prevents a stale compare-delete from deleting a same-payload item
that was deleted and recreated. Existing unwrapped Keychain values remain
readable and are upgraded to an envelope by the next write; reads remain
side-effect free.
Every adapter write atomically updates the secret data and
`kSecAttrGeneric=SHA-256(stored envelope)` in one `SecItemUpdate`. Conditional
deletion is one `SecItemDelete` query matching class, service, account, and that
verifier, so an in-place concurrent write or delete-and-recreate cannot be
removed by a stale request. Existing untagged legacy items remain readable but
fail closed for conditional deletion; their next normal write atomically adds
the verifier. Native password content is released with
`SecKeychainItemFreeContent`; retained item and Core Foundation references are
released with `CFRelease` on success and every failure path. Mutable ctypes
service, account, seed, token, and verifier buffers are zeroed in `finally`
blocks immediately after each native call. Python `bytes` and `str` objects are
immutable, so their interpreter-managed copies cannot be reliably zeroed; the
implementation avoids additional conversions where the API allows, but does
not claim full process-memory zeroization.

The device identity is a stable Ed25519 key. Its private seed is stored in a
separate versioned Keychain item bound to the stable Connector instance ID.
Callers can only obtain the algorithm, public key, SHA-256 fingerprint, opaque
key handle, challenge signature, and handle-bound deletion operation. A
concurrent first-create never overwrites the winning key. Missing, corrupt, or
unavailable Keychain state fails closed and is not regenerated over the
existing item. Deletion uses the atomic verifier-bound `SecItemDelete` query;
if another process updates K1 in place or deletes K1 and recreates K2 under the
same service/account, stale deletion fails and the current value survives.

Pairing is tenant-neutral until the owner confirms it in Cloud. The create
request contains only the stable Connector instance ID, display/platform/version
metadata, Ed25519 algorithm, and public key. The human pairing code is emitted
only by `pair start`. The pairing-offer secret is kept in a dedicated temporary
Keychain item, is sent only in `X-Hermes-Pairing-Offer`, and is removed when the
Connector next observes expiry, observes a terminal server state, is locally
cancelled, or reaches an active binding. Pairing code, offer secret, signing
payload, signature, and access token are excluded from logs, projections, and
object representations. Pairing operations are serialized in-process and
across processes with a private `0600` command lock held over the complete
start, status, or cancel lifecycle, including Cloud calls and
Keychain/projection publication. Acquisition uses non-blocking flock attempts
with bounded monotonic asynchronous polling; timeout, cancellation, and SIGINT
never leave a background waiter that can acquire the lock later.
Temporary-secret removal is SHA-256 compare-delete, and projection removal is
conditional on the original offer ID, so stale cleanup cannot delete a newer
offer. Projection save and conditional delete additionally share a private
`0600` per-projection lock across adapter instances and processes; the stored
offer is re-read, compared, and removed while that exclusive lock is held.

`paired.json` is a private `0600` non-secret projection of the
server-authoritative binding. It contains tenant, device, credential, Agent,
scopes, key handle/fingerprint, token expiry, and lifecycle state. A generic
`DEVICE_AUTH_UNAVAILABLE` response clears the short-lived token and sets
`auth_blocked`; it is not classified as revocation and cannot trigger automatic
re-pairing. Only WebSocket policy close code `1008` together with exact reason
`device_authorization_revoked` or `device_authorization_suspended` persists the
corresponding lifecycle and disables reconnect. Any other close remains an
ordinary reconnectable disconnect and does not change lifecycle.

## Required configuration

All values below are non-secret configuration or file references:

| Environment variable | Meaning |
|---|---|
| `HERMES_CONNECTOR_CLOUD_ENDPOINT` | Absolute `wss://` Connector Gateway endpoint |
| `HERMES_CONNECTOR_API_ENDPOINT` | Absolute `https://` Cloud API base endpoint |
| `HERMES_CONNECTOR_DISPLAY_NAME` | Optional pairing display name; defaults to `Hermes Connector` |
| `HERMES_CONNECTOR_PROFILE` | Local Hermes Agent profile |
| `HERMES_CONNECTOR_VERSION` | Connector semantic version |
| `HERMES_HOME` | Optional absolute, canonical, non-symlinked Hermes Plugin home used to derive missing gateway role paths |
| `HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR` | Optional per-field override for the absolute private generic Local Gateway registry directory |
| `HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR` | Optional per-field override for the absolute private generic Local Gateway socket directory |
| `HERMES_CONNECTOR_CONTROL_REGISTRY_DIR` | Optional per-field override for the absolute private control gateway registry directory |
| `HERMES_CONNECTOR_CONTROL_SOCKET_DIR` | Optional per-field override for the absolute private control gateway socket directory |
| `HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR` | Optional per-field override for the absolute private Observer gateway registry directory |
| `HERMES_CONNECTOR_OBSERVER_SOCKET_DIR` | Optional per-field override for the absolute private Observer gateway socket directory |
| `HERMES_CONNECTOR_STATE_DIR` | Absolute private Connector state directory |
| `HERMES_CONNECTOR_DATABASE_FILE` | Absolute SQLite database path |
| `HERMES_CONNECTOR_LOCK_FILE` | Absolute single-instance lock path |
| `HERMES_CONNECTOR_CREDENTIAL_STORE` | Optional `keychain` (default) or explicit one-shot `file` migration mode |
| `HERMES_CONNECTOR_TOKEN_FILE` | Required only by the explicit one-shot migration command |

`HERMES_CONNECTOR_CONFIG_FILE` may reference a private `0600`, regular,
non-symlink JSON file containing the corresponding lower-case field names.
Environment variables override JSON values. The JSON file may contain
`credential_store`; it may contain `token_file` only when that store is
explicitly `file`, and it may never contain token material.

Each gateway role path is resolved independently with the priority environment
variable, corresponding lower-case JSON field, then current-user default. An
explicit value for one role does not disable defaults for the other five. When
`HERMES_HOME` is absent, the registry root uses the canonical current-user home
plus `.hermes`. When set, `HERMES_HOME` is consumed as an absolute path without
user expansion; tilde-prefixed and relative values are rejected. Missing paths
use:

- registry directories: `$HERMES_HOME/runtime/local-gateways`,
  `$HERMES_HOME/runtime/control-gateways`, and
  `$HERMES_HOME/runtime/observer-gateways`;
- socket directories: `<canonical-/tmp>/hermes-local-gateway-<effective-uid>`,
  `<canonical-/tmp>/hermes-control-<effective-uid>`, and
  `<canonical-/tmp>/hermes-observer-<effective-uid>`.

A `HERMES_HOME` shaped as `$ROOT/profiles/<profile>` is validated as an
absolute canonical path before its registry default root is folded to
`$ROOT/runtime`. On macOS, `/tmp` is resolved before socket defaults are built,
so the resulting paths use the canonical `/private/tmp` root rather than the
`/tmp` symlink spelling.

After per-field resolution, all six gateway role paths must be absolute,
canonical, non-symlinked, within native component and full-path budgets, and
pairwise distinct by both name and physical device/inode identity. Formal
runtime validation repeats the symlink, budget, and physical-isolation checks
after configuration load so a post-load alias or replacement fails closed.
Legacy generic `HERMES_CONNECTOR_REGISTRY_DIR` /
`HERMES_CONNECTOR_SOCKET_DIR` and JSON `registry_directory` /
`socket_directory` are rejected explicitly; there is no implicit migration or
shared-directory fallback.

Plaintext access-token environment variables and CLI arguments are rejected.
Every mode rejects configured tenant/device/credential/Agent/scope authority;
those values come only from the Cloud-confirmed paired projection. The file
adapter is retained only as the source of a controlled one-shot migration. It
requires an existing active server-authoritative paired projection and a legacy
token-file reference, but never reads or copies that legacy token. The
Connector validates the reference, requests a new device challenge, signs it
with the paired Ed25519 identity, verifies the returned binding and expiry, and
stores only the freshly issued device-bound token in Keychain. File mode is
never accepted by formal `run` or pairing commands. The referenced file must be
absolute, regular, non-symlink, non-empty, owned by the current user, bounded in
size, and have permissions no wider than `0600`. Neither logs nor error
messages include its content.

Create the state, registry, and socket directories as private directories
before validation:

```sh
install -d -m 700 /absolute/path/to/connector-state
install -d -m 700 /absolute/path/to/local-gateway-registry
install -d -m 700 /absolute/path/to/local-gateway-sockets
install -d -m 700 /absolute/path/to/control-registry
install -d -m 700 /absolute/path/to/control-sockets
install -d -m 700 /absolute/path/to/observer-registry
install -d -m 700 /absolute/path/to/observer-sockets
# Migration/test mode only:
install -m 600 /secure/source/cloud-token /absolute/path/to/cloud-token
```

## Validation and execution

From `hermes-connector/`, validate configuration and paths without opening a
local socket, connecting to Cloud, reading/writing/deleting a Keychain item, or
creating the identity, database, or lock files:

```sh
uv run --locked python -m hermes_connector.cli --check
```

Before distributing Connector, use the build-first release gate. It removes
only prior `hermes_connector-*.whl` and `hermes_connector-*.tar.gz` targets,
builds the real artifacts into `dist/`, and then inspects those exact wheel and
sdist files for source parity, descriptor-v1 rejection, the complete generated
Observer v2 policy/schema family, required safety modules, isolated artifact
loading, and absence of test harnesses:

```sh
uv run --locked python scripts/build_and_verify_dist.py
```

Formal service execution requires an active paired projection, ready local
Hermes authority, the role-specific private endpoints, and valid Cloud
credentials:

```sh
uv run --locked python -m hermes_connector.cli run
```

On 2026-08-01, the read-only live gate against the currently running local
Hermes 0.19.0 instance returned incompatible status because
`gateway_extension_capabilities`, `gateway_extension_spi_version`, and
`register_gateway_extension` are absent. The current UID-501 Local and Control
registries contained no descriptor, all three role socket directories contained
no socket, and the Observer registry contained only three descriptors whose
publisher PIDs were no longer live. Connector discovery returned no trusted
endpoint and left those files untouched. This is the expected fail-closed result,
not evidence of a production Agent connection.

Create, inspect, or locally abandon a formal pairing offer:

```sh
uv run --locked python -m hermes_connector.cli pair start
uv run --locked python -m hermes_connector.cli pair status
uv run --locked python -m hermes_connector.cli pair cancel
```

`pair start` prints only the human code, public credential fingerprint, and
expiry. `pair status` prints the lifecycle/activation state, fingerprint, and
expiry. `pair cancel` abandons the local offer and deletes its temporary
Keychain credential; owner-authorized server cancellation remains a Cloud-side
operation.

Use a validated legacy file reference to mint a fresh device-bound Keychain
token only after an active paired projection already exists:

```sh
uv run --locked python -m hermes_connector.cli credential migrate-file
```

The command prints only completion, device ID, and credential fingerprint. It
does not read or disclose the legacy token and does not compose the formal
runtime.

`SIGINT` and `SIGTERM` request the same bounded Supervisor drain and stop path.
The process lock covers the complete supervised lifetime.

The macOS lifecycle gate repeats the public `build_service_runner` path 100
times with real SQLAlchemy SQLite startup/migration, a real length-prefixed UDS
Local Gateway handshake, and a real macOS process lock. After every stop it
requires Connector tasks, file/socket descriptors, the SQLite worker/engine,
accepted UDS peers, and the flock to return to their baseline; after all 100
cycles it also requires the process thread set to return to baseline. The gate
does not use polling sleeps to hide delayed cleanup.

The Observer transport gate separately repeats 100 real dedicated-descriptor
discovery plus WebSocket-over-UDS subscribe/snapshot/unsubscribe lifecycles. It
requires every server connection to close and the task, thread, file descriptor,
and socket sets to return to their warmed baseline after every cycle; closing
the discovery client also returns its worker and descriptors to the pre-client
baseline. A focused integration path proves snapshot-before-live ordering.

On first pairing or one-shot migration, the Connector atomically publishes
`STATE_DIR/instances.json` with mode `0600`. Formal `run` publishes it only
after the zero-side-effect Local preflight succeeds. It contains distinct
stable Connector and local-client UUIDs and no secret. Concurrent first
starters converge on the same file. A malformed, replaced, symlinked, wrongly
owned, or wrongly permissioned identity file fails closed and is never
regenerated.

Formal `run` additionally requires a valid active `STATE_DIR/paired.json`.
Unpaired, auth-blocked, suspended, or revoked projections fail closed. The
one-shot file migration also requires that active projection and never starts
the runtime.

SQLite runtime access, including Observer outbox persistence and ACK/NACK
settlement, remains through SQLAlchemy ORM sessions. Only the existing
centralized SQLite connection-policy module may issue fixed PRAGMA statements.
