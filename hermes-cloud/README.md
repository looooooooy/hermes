# Hermes Cloud

Hermes Cloud is the authoritative server-side session and identity plane for
Hermes clients and Connectors. The Business API and Connector Gateway share
the same domain and application contracts while retaining separate runtime
entrypoints.

## Persistence contract

Application database access is ORM-only. Runtime repositories use SQLAlchemy,
and schema changes use the published typed migration catalogs. Operational
scripts must call those repositories and migration APIs; handwritten SQL and
database-specific command-line writes are outside the supported boundary.

The current schema heads are:

- SQLite v13: `0013_session_catalog_recovery`.
- PostgreSQL v13: `0013_session_catalog_recovery`.

SQLite local storage is a server-authoritative store for the test-server
profile. Android storage remains a client projection and does not become the
session authority.

## Observer contract selection

Connector transport continues to use Cloud Envelope `contract_version=1`.
Observer output-parity v2 is a payload/message-family selection activated only
when both peers negotiate `session.observe.output-parity.v1`; it does not change
the Cloud envelope version. Unavailable capability stays exact Observer v1,
and an active connection never changes Observer version in place. Business API
clients select v2 explicitly in a single-use WebSocket ticket and then use only
`hermes.tui.v2`.

Durable subscription intents freeze the first dispatched Observer contract,
wire message type, and canonical payload digest. A reconnect may replay that
exact binding, but cannot reuse the same transport identity under another
Observer contract.

## Health and readiness

Component lifecycle and dependency readiness are separate state dimensions.
A component enters lifecycle `READY` after its own startup completes, but the
external `/ready` route returns 200 only while every critical dependency probe
is healthy. A transient critical timeout or error therefore returns 503 and
retains a safe dependency diagnostic without converting the component into a
fatal lifecycle failure.

Dependency probes run sequentially at the bounded refresh interval. Monitoring
continues after failures, including a failure in the initial startup probe, so
readiness returns automatically when the dependency recovers. Shutdown cancels
and joins the monitor before lifecycle stop, preventing a late probe from
reviving readiness. Fatal component exceptions still enter terminal `FAILED`;
healthy dependency refreshes cannot clear or recover that state.

## Browser realtime authentication

`POST /api/auth/ws-ticket` accepts the native Bearer flow and the browser
`hermes_session_at` HttpOnly cookie. Cookie-only requests require an HTTPS
`Origin` that exactly matches the effective Host. The packaged ASGI launcher
trusts proxy headers only from loopback, and the packaged nginx include forwards
Host and scheme explicitly. Conflicting or invalid Bearer/cookie credentials
fail closed; tokens are never returned by password login or written to logs.

## Browser session catalog and logout

The H5 session catalog uses `credentials: include`. Its canonical routes are
`GET /api/v1/agents` and `GET /api/v1/agents/{agent_id}/sessions`; the previous
`GET /api/agents` and list-form `GET /api/sessions?agent_id=...` routes remain
deprecated compatibility aliases. Every directory query key is an allowlisted
singleton; unknown or repeated values fail with `400`, and the canonical
Agent-scoped route rejects query-form `agent_id`. Cookie authentication is limited to those directory
reads; session detail and transcript routes remain native Bearer-only. An HTTPS catalog read
must provide either an exact same-origin `Origin` or the browser Fetch Metadata
triple `Sec-Fetch-Site: same-origin`, `Sec-Fetch-Mode: cors`, and
`Sec-Fetch-Dest: empty`. If Bearer and cookie credentials are both present,
they must resolve to the same active server-side refresh session.

The Agent-scoped session route is catalog-only. It lists committed
`session.catalog.v1` records with their stable opaque `session_id`, profile,
runtime generation, surface, revision, availability, and allowed actions. It
does not manufacture transcript messages, running state, or controller state;
unknown catalog lineage fails closed.

`POST /auth/logout` accepts an empty, exact same-origin HTTPS request. It
durably revokes the refresh session through the identity ORM repository before
returning exactly `{"ok": true}` and expiring the access, refresh, and provider
HttpOnly cookies. Repeating logout with the same signed cookies, or with no
cookies after the browser has cleared them, is successful. Malformed or
conflicting credentials fail closed; a database failure leaves the cookies and
session active so the caller can retry.

Forwarded HTTPS is accepted only when the direct peer is listed in
`trusted_forwarded_proxy_hosts`; this setting accepts unique loopback IP
addresses only, and the request must contain exactly one
`X-Forwarded-Proto: https` header. This H5 closure reuses the existing refresh
session model and therefore adds no schema or migration revision.

Single-use ticket consumption uses one shared SQLAlchemy mapped-expression
predicate for SQLite and PostgreSQL. The SQLite suite exercises the same
predicate against a real temporary database; PostgreSQL coverage compiles that
predicate with the PostgreSQL dialect and is not presented as a live database
integration run. A future PostgreSQL deployment must pass the real PostgreSQL
transaction/concurrency behavior suite before transactional parity is claimed.

## Operations

- General test-server release, systemd, nginx, backup, validation, and rollback
  contract: `deploy/test_server/README.md`.
- SQLite production-shaped profile, ORM migration history, permissions, and
  canary contract: `deploy/test_server/sqlite/README.md`.
- Dry-run-first ORM retirement of the explicit legacy test-server session:
  `deploy/test_server/scripts/cleanup_test_seed_session.py`.
- SQLite migration catalog details:
  `src/hermes_cloud/platform/sqlite/README.md`.

The SQLite profile enables only Business API and Connector Gateway as runtime
services. Migration, seed, and test-token units are explicit one-shot
operations.
