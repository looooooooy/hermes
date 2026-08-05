# SQLite provider boundary

The provider uses `hermes_cloud.platform.sqlalchemy` for operation-scoped
transactions, identity and projection read behavior, domain mapping, login
resolution, and ORM readiness checks. SQLite-specific modules contain only
storage validation, schema adaptation, provider binding, and dialect-specific
atomic writes.

`hermes_cloud.platform.postgres.models` is the frozen shared catalog
compatibility source. It remains at that published path so the published
PostgreSQL migration checksums and compiled structures stay unchanged. SQLite
may import that model catalog, but it must not import PostgreSQL runtime or
repositories.

SQLite writes target version 3.24 and newer. They use `ON CONFLICT` plus guarded
row counts and same-transaction ORM verification. SQLite repositories do not
use `RETURNING`, raw SQL, or provider PRAGMA statements.

## SQLite migration history

The SQLite catalog currently has eleven real published revisions:
`0001_current_sqlite_baseline`, `0002_observer_projection`,
`0003_observer_authority_and_encryption`, and
`0004_connector_transport_cursor`, and
`0005_connector_handshake_ownership`, `0006_observer_output_parity`,
`0007_observer_inbox_runtime_epoch`,
`0008_observer_subscription_wire_contract`, `0009_observer_inbox_retention`,
`0010_observer_subscription_legacy_wire_repair`, and
`0011_session_projection_durable_identity`. Revision 5 adds expiring Connector handshake
ownership and the durable Observer ACK/NACK receipt outbox. Revision 6 adds the
encrypted, server-authoritative Observer v2 lifecycle side table without
mutating the published v1 projection tables. Revision 7 scopes Connector
sequence idempotency to one runtime generation while preserving the published
revision-6 inbox shape as immutable migration input. Revision 8 freezes the
first dispatched Observer contract, wire type, and payload digest on every
durable subscription intent. Never-dispatched legacy rows remain unbound until
their first reservation. A legacy identity that may already have been sent is
cancelled without guessing v1 or v2; an active/closing target receives a new,
unbound request identity for safe reconciliation.
Revision 9 gives each Observer inbox row an explicit retention boundary and
backfills revision-8 rows from `received_at` with the 30-day legacy policy.
Revision 10 is a typed ORM data repair for databases that ran the originally
published revision-8 backfill. Every exact legacy v1-derived binding enters the
repair review regardless of a conflicting `created_at` clock. A v1 identity is
preserved only when its creation and first dispatch are both strictly after the
revision-8 ledger boundary and its intent/outbox creation, availability,
attempt, publish, and settlement evidence is internally consistent. Otherwise
the migration cancels an unknowable wire identity before issuing at most one
unbound reconciliation identity from the
authoritative target state. The data-only revision leaves the revision-9 schema
shape unchanged, so both revisions share that schema fingerprint while retaining
distinct immutable ledger entries. Revision 11 freezes
`tenant_id + agent_id + profile + session_key` as the durable session identity,
binds session-scoped WebSocket tickets to stable `session_id`, and preserves
message/event/cursor children. A v10 SQLite source upgrades only when
authoritative Observer state uniquely proves `agent_id + profile`; a familiar
test-seed name is not proof. PostgreSQL v10 non-empty session/ticket state fails
closed pending external reconciliation.

Each checksum is the SHA-256 digest of a canonical SQLAlchemy Inspector
structure covering every table, column type,
nullable flag, primary key, foreign key, unique constraint, index, and check
constraint. A read-only ORM mapping of the SQLite 3.24-compatible
`sqlite_master` catalog adds complete canonical table DDL plus trigger, view,
and explicit-index definitions. Table columns and constraints retain their
original order because SQLite constraint order can change conflict behavior
and automatic-index numbering. The bounded canonicalizer is quote, comment,
parenthesis, and ASCII-whitespace aware; ambiguous or unsupported structure
fails closed. SQLite-owned `sqlite_*` objects are excluded.

The typed revision-1 creator gives every `CreateTableOp` a deterministic
constraint order before invoking Alembic, so its complete DDL fingerprint is
stable across fresh Python processes without erasing order from an existing
database. Legacy adoption is not derived from a fresh `metadata.create_all`.
The immutable `20260731T084500Z` source is allowlisted by both its canonical
schema fingerprint and an independent SHA-256 digest of the exact ORM-read
`sqlite_master` rows. The raw digest does not reuse the canonicalizer.

The migration runner uses Alembic typed Operations for DDL and an ORM-mapped
`hermes_sqlite_schema_migrations` ledger; it does not execute business raw SQL.

The supported and automatically verified sources are:

- an empty database, which is created and upgraded through revision 11;
- the unversioned `20260731T084500Z` release only when both immutable source
  signatures match, after which the ORM ledger is adopted;
- the deployed revision-1 compatibility source only when its ORM history is the
  exact published v1 prefix, its business schema matches both frozen
  `20260731T084500Z` canonical/raw signatures, and its ledger matches both the
  frozen canonical and raw v1 ledger signatures;
- the exact deployed `20260731T084500Z-legacy-v1-to-v5` source only when its
  ORM history is the published v1-through-v5 prefix and its complete schema,
  legacy base, ledger, Observer v2/v3 overlay, Connector transport v4 overlay,
  and Connector handshake v5 overlay each match their frozen canonical and raw
  signatures; this source is reported as `versioned-5-compatible`;
- the exact deployed `20260801T131728Z` revision-10 source only when its ORM
  history is the published v1-through-v10 prefix and its complete database
  matches both frozen canonical and raw catalog signatures; this source is
  reported as `versioned-10`;
- other revision 1 through 10 databases whose complete business and ledger
  structure plus ORM history exactly match their published source catalog.

Unknown tables, any structural drift, an empty or malformed ledger, mutated
checksums, raw DDL drift, missing versions, unknown legacy signature pairs, and
future versions fail closed. Inspector shape alone never authorizes the deployed
v1, deployed legacy-v1-to-v5, or deployed revision-10 compatibility source.
Every exception requires its exact immutable source proof; other legacy-derived
versions and any component drift fail closed. The dry-run path performs the
same read-only validation and never creates or adopts a schema.
Schema DDL plus the ORM ledger use an outer transaction, a nested SQLite
savepoint, and an inner ORM savepoint. Empty creation creates the ledger table
first as a transactional guard that acquires SQLite's write lock. On that same
connection, before business DDL or an ORM ledger row, the upgrader revalidates
an empty source, both frozen unversioned legacy signatures, or the complete
four-signature revision-1 compatibility proof, the complete multi-component
revision-5 compatibility proof, or the exact revision-10 ledger prefix and
complete canonical/raw database signature. It validates the complete current
schema and ORM history again before the outer transaction commits; the
legacy-base current fallback continues to require the exact frozen canonical
and raw ledger pair.
Source drift, a ledger flush, or a commit failure
therefore rolls empty creation back to zero tables and legacy adoption back to
the exact unversioned source; both are immediately retryable. Canonical
reference inspection uses only an in-memory database and leaves no temporary
database file.

Concurrent empty creation or legacy adoption remains cross-process-safe without
an in-process mutex. A fixed set of SQLite DDL/ledger collision errors permits
a bounded, condition-based read-only plan revalidation window. Only a fully
valid `current` result is accepted; when the database does not converge before
the fixed deadline, the original collision is propagated. The poll is bounded
and never retries migration writes.

Verified historical SQLite sources are revisions 1 through 10. The recent
historical pair is `(9, 10)`, so `historical_source_count=10` and
`recent_two_covered=true`. PostgreSQL revisions must never be presented as
SQLite history. Catalog length alone never changes the coverage result: a
source version is counted only after its real immutable fixture passes
upgrade-to-current and ORM read/write.

## Deployed revision-10 compatibility evidence

On 2026-08-02, read-only inspection of release `20260801T131728Z` proved that
the deployed database has the exact published revision-1-through-10 ledger.
The deployed complete signature is canonical
`f43658517c47ec0336e7e061ec4ee04aa976f3ee9d91b659a8c35720bb3944be`
and raw
`df2b0f97389e0844c7e0f665b2d4a3caf52b460d4b94551a74bd34ccebd54820`.
Its tables, indexes, columns, and Inspector constraint shapes equal the
revision-10 catalog replay; the difference is the preserved rev1 table-
constraint order in `sqlite_master`. Candidate `20260801T222642Z` previously
rejected this exact source because its revision-10 catalog contained only the
deterministically ordered signature pair.

Revision 11 now carries a separate, strict versioned-database compatibility
entry for that exact double signature. A one-field schema variant and a ledger
checksum variant remain rejected, while an isolated exact fixture plans as
`versioned-10`, upgrades through typed Alembic operations and ORM ledger writes,
and then plans as `current` at revision 11. This is local compatibility and test
evidence only: no release was built or uploaded, no remote migration ran, and
no service was restarted or deployed for this fix.

`observer_inbox_messages` is a bounded transport-idempotency window, not a
permanent ledger. New rows receive the same tenant retention boundary as their
accepted Observer frame. Cleanup selects one tenant and one bounded batch,
retains unexpired/current-epoch rows, removes expired rows through ORM
transactions, and rolls the whole inbox batch back on failure so the next run
can retry safely.
