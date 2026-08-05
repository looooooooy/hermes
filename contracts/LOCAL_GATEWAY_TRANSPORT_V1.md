# Local Gateway Transport v1

## 1. Scope and authority

This document freezes the local transport between Hermes Connector and the
Hermes Agent Plugin. `local-gateway-transport-v1.json` is the machine-readable
profile, and `schemas/local/gateway-discovery-v1.schema.json` is the discovery
descriptor contract. The JSON request and response bodies remain the
platform-neutral `local.hello` and `local.welcome` contracts.

POSIX implementations use a Unix domain **stream** socket (UDS). One accepted
connection carries exactly one request and one response. Neither peer may
pipeline a second request, multiplex requests, or reuse the connection.

## 2. Frame

Every request and response is encoded as exactly two consecutive parts:

```text
+-------------------------------+---------------------------+
| 4-byte unsigned big-endian N  | N bytes UTF-8 JSON body  |
+-------------------------------+---------------------------+
```

`N` is the byte length of the encoded body, not a character count. It must be
in the inclusive range `1..262144`. A receiver reads exactly four prefix bytes,
checks `N` before allocating the body buffer, then reads exactly `N` body bytes.
The body must decode as strict UTF-8 and then as one JSON object. The JSON
decoder must reject duplicate object member names at every nesting level.

A receiver fails closed, closes the connection, and performs no business
effect when it encounters any of the following:

- a zero-length body;
- a body larger than 262,144 bytes;
- a truncated prefix or truncated body;
- a non-UTF-8 body;
- a duplicate JSON key;
- a body that is not a JSON object or does not match its message schema;
- a second request frame on the same connection.

Transport completion is not a business acknowledgement. A response is valid
only after the complete frame and the selected JSON body schema both pass.

### 2.1 Error response and dispatch

A rejected request may receive one error response with exactly this shape:

```json
{"error":{"code":4304,"reason":"capability_not_available"}}
```

The complete body must validate against
`schemas/local/gateway-error-v1.schema.json`. `code` must be a code declared in
`error-codes-v1.json`, and `reason` must be the catalog `name` paired with that
same code. The body and nested error object reject all unknown fields. They
must not contain diagnostic details, free-form messages, stack traces,
exception text, paths, credentials, request echoes, or internal identifiers.
Diagnostics belong only in access-controlled local logs with their own
redaction policy.

Codes `4300..4306` may be produced by the Local Gateway handshake or transport
when their catalog meaning applies. Codes `4307..4309` are schema-recognized
but may be produced only by an operation whose contract admits the catalog
meaning; their inclusion in the shared catalog is not blanket permission for a
handshake implementation to emit them.

An error body uses the same four-byte framing, UTF-8, size limit, strict JSON,
and one-response lifecycle as a successful body. After decoding the response
object, the Connector dispatches an exact top-level `error` object to the error
Schema. A response containing `error` must not be parsed as `local.welcome`,
and a malformed error must fail closed rather than fall through to welcome
parsing. A response without `error` is accepted only if it validates as
`local.welcome`. Receiving a valid error closes the connection and does not
establish a Local Gateway session.

## 3. Discovery descriptor

The descriptor has exactly these top-level fields:

```json
{
  "version": 1,
  "pid": 4312,
  "profile": "default",
  "socket_path": "/absolute/path/to/gateway.sock",
  "instance_id": "89e2f4b1-3f0a-4d55-9c17-720bca24e6e1"
}
```

Unknown top-level fields, including platform fields, are rejected. The
descriptor has no extension namespace. `socket_path` is an absolute POSIX
path; an adapter must additionally enforce the byte limit of its native UDS
address structure.

### 3.1 Publisher rules

The Plugin creates one private run directory per effective user and profile.
The directory must be owned by the effective user, must be a real directory
with mode `0700`, and must have no symlink in the trusted descriptor/socket
path. The Plugin binds and starts listening on the UDS, sets the socket mode to
`0600`, verifies that it owns the socket, and only then publishes the
descriptor.

Descriptor publication is atomic:

1. create a new regular temporary file in the same private parent directory
   without following a symlink;
2. set mode `0600` before writing sensitive content;
3. write one strict UTF-8 JSON descriptor, flush it, and sync it;
4. atomically replace the descriptor path with the temporary file;
5. sync the parent directory.

The descriptor is never published before the socket is listening. Startup
failure removes only resources created by that startup attempt.

### 3.2 Consumer validation order

The Connector validates discovery without following links:

1. `lstat` the parent; require a real directory, effective-user ownership,
   exact mode `0700`, and no symlink;
2. `lstat` the descriptor; require a regular file, effective-user ownership,
   exact mode `0600`, and no symlink;
3. open the descriptor relative to the verified parent with no-follow semantics,
   read a bounded file from that same descriptor, repeat `fstat`, and require
   unchanged device, inode, modification time, size, owner, mode, and regular-file
   type before rejecting duplicate keys and validating the v1 Schema;
4. require the descriptor PID to be live at validation time (`EPERM` from a
   POSIX signal-zero probe counts as live; `ESRCH` does not);
5. `lstat` `socket_path`; require a socket, effective-user ownership, exact
   mode `0600`, and no symlink;
6. connect and complete the framed `local.hello` / `local.welcome` exchange.

Failure at any step invalidates discovery. An implementation may remove a
stale descriptor or socket only after repeating the ownership, type, mode, and
identity checks immediately before deletion. Shutdown removes the descriptor
only if its `instance_id` still belongs to the shutting-down Plugin, and removes
the socket only if it is still the socket created by that Plugin instance.

## 4. Deadline, cancellation, and cleanup

Every client exchange has one finite caller-configured timeout measured with a
monotonic clock. The deadline covers discovery, validation, connect, complete
request write, complete response read, validation, and close; completing one
phase does not reset it.

On timeout or cancellation, the client closes the connection immediately and
releases every open file, socket, buffer, task, and waiter. The server also
closes the accepted connection on all success, protocol-error, timeout, and
cancellation paths. A timeout or cancellation after request transmission began
does not prove that the peer failed to receive the request; callers must not
blindly retry a future state-changing message without its own idempotency key.

The client lifecycle is:

```text
DISCOVERING --> VALIDATING --> CONNECTING --> EXCHANGING --> CLOSED
      |              |              |              |
      +--------------+--------------+--------------+
                     timeout / cancellation / error
                                  |
                                  v
                                CLOSED
```

`CLOSED` is terminal for that connection. A retry starts a new discovery and a
new connection; it does not reuse transport state.

## 5. Windows adapter boundary

Windows Named Pipe support is reserved for a later transport adapter. That
adapter may replace POSIX discovery and socket I/O with Windows-native secure
primitives, but it must not change the JSON body, message schema, capability
semantics, duplicate-key rejection, body size limit, or one-request lifecycle.
The same four-byte frame SHOULD be preserved; any future incompatible framing
requires a new transport contract version rather than a platform field in the
JSON body.
