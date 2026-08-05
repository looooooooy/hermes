# Cloud SQLite ORM Production Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use $superpower-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking via update_plan.

**Status:** Round 13 implementation is in verification. Independent review is pending on 2026-07-31.

**Goal:** Add a deployable, file-backed SQLite ORM provider for the remote Hermes Cloud test server while preserving the published PostgreSQL model and migration contract.

**Architecture:** Keep the published PostgreSQL mappings in place. Add SQLite-local engine, schema-clone, repository, runtime, migration, and deployment adapters under `hermes_cloud.platform.sqlite`; reuse dialect-neutral identity/query behavior without putting SQLite branches in `platform/postgres`. Production composition selects only explicitly supported PostgreSQL or SQLite URL schemes and fails closed for every other URL.

**Tech Stack:** Python 3.11+, SQLAlchemy 2.x ORM/Core expressions, SQLite dialect insert, FastAPI/TestClient, pytest, systemd drop-ins.

---

### Task 1: Freeze the PostgreSQL publication contract

**Files:**
- Read: `hermes-cloud/src/hermes_cloud/platform/postgres/models.py`
- Read: `hermes-cloud/src/hermes_cloud/platform/postgres/catalog.py`
- Test: `hermes-cloud/tests/migration/platform/postgres/`

- [x] Run the complete PostgreSQL migration suite before any shared mapping edit.
- [x] Capture the seven published checksums, structural digests, and PostgreSQL DDL compilation output from the existing tests.
- [x] Prefer SQLite-local compiler/metadata adaptation. No shared mapped type change was required.

Run:

```bash
cd hermes-cloud
PYTHONDONTWRITEBYTECODE=1 uv run --locked pytest -q -p no:cacheprovider tests/migration/platform/postgres
```

Expected: all PostgreSQL migration tests pass before and after the SQLite slice.

### Task 2: File-backed SQLite engine and schema

**Files:**
- Create: `hermes-cloud/src/hermes_cloud/platform/sqlite/__init__.py`
- Create: `hermes-cloud/src/hermes_cloud/platform/sqlite/engine.py`
- Create: `hermes-cloud/src/hermes_cloud/platform/sqlite/schema.py`
- Test: `hermes-cloud/tests/platform/sqlite/test_engine.py`
- Test: `hermes-cloud/tests/platform/sqlite/test_schema.py`

- [x] Write failing tests requiring `sqlite+pysqlite:////absolute/path`, rejecting memory/relative/symlink/unsafe parent/permissions wider than `0660`.
- [x] Verify RED because the SQLite provider does not exist.
- [x] Implement a redacted `SQLiteConfigurationError`, safe path resolver, and engine builder using:

```python
connect_args = {"check_same_thread": False, "timeout": 5.0}
execution_options = {"schema_translate_map": SQLITE_SCHEMA_TRANSLATE_MAP}
```

- [x] Write failing tests that clone all mapped tables into schema-less metadata, adapt PostgreSQL UUID/JSONB only for SQLite compilation, and filter only checks containing PostgreSQL-only `~`, `jsonb_typeof`, or `octet_length`.
- [x] Implement SQLite-local SQLAlchemy compiler adapters and `build_sqlite_metadata()`; call `MetaData.create_all` only from the explicit migration adapter.
- [x] Enable `PRAGMA foreign_keys=ON` through one SQLAlchemy connect event for every pooled DBAPI connection, close the policy cursor in `finally`, and reject orphan ORM rows with transaction rollback.
- [x] Verify the real temporary database contains flattened table names and no attached-schema names.

### Task 3: SQLite repositories, operation scope, and readiness

**Files:**
- Create: `hermes-cloud/src/hermes_cloud/platform/sqlite/repositories/__init__.py`
- Create: `hermes-cloud/src/hermes_cloud/platform/sqlite/repositories/identity.py`
- Create: `hermes-cloud/src/hermes_cloud/platform/sqlite/repositories/projection.py`
- Create: `hermes-cloud/src/hermes_cloud/platform/sqlite/runtime.py`
- Test: `hermes-cloud/tests/platform/sqlite/test_repositories.py`
- Test: `hermes-cloud/tests/platform/sqlite/test_runtime.py`

- [x] Write failing real-file tests for identity credential lookup, refresh create/rotate/revoke, projection upsert/idempotency/regression, ACL list/detail/transcript, transaction rollback, and session closure.
- [x] Verify RED for rowcount-success paths that returned before ORM re-read.
- [x] Reuse dialect-neutral identity behavior through a SQLite-named adapter.
- [x] Implement projection upserts with `sqlalchemy.dialects.sqlite.insert`; do not import PostgreSQL insert from SQLite modules.
- [x] Implement operation-scoped SQLite repositories and an independent critical readiness probe named `sqlite`.
- [x] Make refresh rotation/revocation and WebSocket ticket consumption perform same-transaction ORM pre-read and post-read with `populate_existing`, validate the complete resulting state, and classify zero-row replay/conflict outcomes without raw SQL or `RETURNING`.
- [x] Verify each call opens one transaction and releases it on success and error.

### Task 4: Explicit production provider selection

**Files:**
- Modify: `hermes-cloud/src/hermes_cloud/adapters/business_api_runtime.py`
- Test: `hermes-cloud/tests/entrypoints/business_api/test_production_composition.py`

- [x] Write failing tests that select PostgreSQL for `postgresql`/`postgresql+psycopg`, SQLite for `sqlite`/`sqlite+pysqlite`, and return ready `503` for unknown schemes.
- [x] Write a failing real-file SQLite readiness and engine-disposal test.
- [x] Implement scheme parsing before engine creation, preserve existing PostgreSQL options, and use SQLite-specific engine/runtime/probe composition.
- [x] Verify errors and health payloads contain no DSN, database path, signing secret, or exception text.

### Task 5: Dry-run-first SQLite migration and provider-aware seed

**Files:**
- Create: `hermes-cloud/deploy/test_server/scripts/migrate_sqlite.py`
- Modify: `hermes-cloud/deploy/test_server/scripts/seed_test_data.py`
- Create: `hermes-cloud/deploy/test_server/tests/test_migrate_sqlite.py`
- Modify: `hermes-cloud/deploy/test_server/tests/test_seed_test_data.py`

- [x] Write failing tests proving migration default mode creates no file and `--apply` creates all ORM tables with database mode `0660`.
- [x] Implement a redacting parser and SQLite migration runner that invokes only `build_sqlite_metadata().create_all(...)`; no Alembic/PostgreSQL roles/raw SQL.
- [x] Write failing tests proving seed plan/apply use the same SQLite provider engine and one ORM transaction.
- [x] Refactor seed engine selection through the validated provider builder while preserving PostgreSQL behavior and existing dry-run/apply output.
- [x] Verify interrupted/error paths dispose engines and never print credentials, DSNs, database paths, or password material.

### Task 6: Provider-specific deployment package

**Files:**
- Create: `hermes-cloud/deploy/test_server/sqlite/README.md`
- Create: `hermes-cloud/deploy/test_server/sqlite/env/test-server.env.example`
- Create: `hermes-cloud/deploy/test_server/sqlite/scripts/preflight.sh`
- Create: `hermes-cloud/deploy/test_server/sqlite/nginx/hermes-test-server.conf`
- Create: `hermes-cloud/deploy/test_server/sqlite/systemd/hermes-cloud-sqlite-migrate.service`
- Create: `hermes-cloud/deploy/test_server/sqlite/systemd/hermes-cloud-sqlite-seed-test-data.service`
- Create: `hermes-cloud/deploy/test_server/sqlite/systemd/hermes-cloud-sqlite-business-api.service`
- Create: `hermes-cloud/deploy/test_server/sqlite/systemd/hermes-cloud-sqlite-connector-gateway.service`
- Test: `hermes-cloud/deploy/test_server/tests/test_sqlite_deploy_artifacts.py`

- [x] Write failing artifact tests requiring a separate SQLite directory, file-DSN secret references, a shared group-writable state directory, migration/seed `UMask=0007`, explicit oneshots, and no runtime dependency on migration/seed.
- [x] Add independent provider-specific runtime and oneshot units plus formal operator steps without editing existing PostgreSQL units, environment examples, Nginx, or remote state.
- [x] Verify runtime, migration, and seed resolve the same operator-provisioned absolute file DSN; units contain only absolute secret-file references, never credential values.

### Task 7: Real persistent-file vertical E2E

**Files:**
- Create: `hermes-cloud/deploy/test_server/tests/test_sqlite_business_api_e2e.py`
- Modify: `hermes-cloud/tests/platform/sqlite/test_sqlite_architecture.py`

- [x] Write the persistent-file E2E using one temporary real SQLite file:
  1. migration plan leaves the file absent;
  2. migration apply creates the schema;
  3. seed plan leaves ORM `select(func.count())` row counts unchanged;
  4. seed apply creates the deterministic account/session/transcript;
  5. production builder reaches ready;
  6. password login succeeds;
  7. refresh rotates the token;
  8. a WebSocket ticket is consumed once and rejected on reuse;
  9. shutdown and a fresh builder preserve refresh, session list/detail, and transcript data.
- [x] Record RED for the projection ORM re-read contract and missing AST gate. The expanded dry-run/one-use-ticket E2E was coverage-only and passed immediately against the existing provider behavior.
- [x] Add only the minimal production behavior required to make the complete E2E green.
- [x] Add an AST scan over SQLite provider, neutral SQLAlchemy runtime/repositories, migration, and seed files rejecting sqlite3 CRUD/cursors, `text`, `exec_driver_sql`, raw SQL string execution, and `RETURNING`; allow only the exact central SQLite PRAGMA policy and ignore docstrings/messages.
- [x] Close AST bypasses with a positive-proof repository policy guard. At each recognized SQLAlchemy Session/Connection statement sink, both the receiver and statement require independent source proofs. Receiver proof originates only from the real SQLAlchemy export modules for `Session`, `AsyncSession`, `Connection`, `AsyncConnection`, `sessionmaker`, and `async_sessionmaker`, then propagates through receiver annotations, constructors, factories, assignments and aliases, instance attributes, context-manager entry, SQLAlchemy receiver subclass MROs, or the explicit in-repository SQLAlchemy repository-base summary; variable names never prove either SQL or non-SQL receiver identity. A bound statement sink is itself a proof value carrying its receiver and method, so direct calls, bound aliases, constant-name `getattr`, and awaited async aliases all pass through the same receiver and statement checks. Statement proof has only two outcomes: `ORM_CORE` and `UNPROVEN`; an annotation describes a static type but never upgrades an unknown statement value. Only expression values statically derived from approved SQLAlchemy ORM/Core constructor imports and the explicit statement-preserving method allowlist are admitted. Star imports, non-existent or foreign modules, same-name local or foreign callables, unknown values, mixed unions, dynamic callable or method effects, unprovable generator/loop/conditional results, strings, `text`, `literal_column`, and other textual or non-ORM values are rejected. Unknown or external function decorators make the decorated function result `UNPROVEN`; only the exact built-in `staticmethod`/`classmethod` and `abc.abstractmethod` preserving decorators are allowed. Raw-string and forbidden-API findings remain auxiliary diagnostics, not the safety decision. The bounded analysis implements only the finite proof rules required by current Cloud production: statement order and lexical ownership, deterministic branch and BoolOp pruning, isolated unknown `IfExp` and callable-union effects, finite sequence and empty-loop order, `while False`, generator leftmost-iterable creation plus deferred consumption, monotonic branch object references, and statically resolved C3 `super()` dispatch. The scanner never executes source, scans every in-scope production module with zero findings, and conservatively rejects unsupported dynamic Python. It is a repository policy guard, not a general Python interpreter and not a claim of Python semantic completeness.
- [x] Close the final higher-order and argument-provenance bypasses. Only exact standard-library `functools.partial`, `operator.methodcaller`, and `operator.attrgetter` carry callable proof; they preserve the receiver, method, prebound positional values, and prebound keyword values until final invocation. Exact `property` descriptors propagate the getter result when read from an instance. Every approved statement constructor, expression constructor, and statement-preserving method now composes argument provenance: explicit Hermes ORM models, tables, columns, ORM expressions, and proven bound values remain safe, while unknown/raw predicates, external `TextClause` values, textual builders, and `column(..., is_literal=True)` fail closed. `Annotated` reads only its first type argument; `Optional` and `Union` merge all receiver alternatives without using metadata as proof. Narrow path-and-function contracts cover only the existing generic internal seed/projection helper boundaries during fallback analysis; explicit analyzed SQL-bearing call arguments still replace those defaults, and identical helper names outside those exact files receive no trust.
- [x] Normalize nested callable proof recursively. Exact `partial`, built-in `getattr`, dotted-path `attrgetter`, `methodcaller("__getattribute__", ...)`, and `__call__` extraction compose through one recursive invocation path, so a SQL sink callable retains its receiver, sink method, and every prebound argument until the terminal call. Unknown external wrappers receiving a proven SQL receiver or sink callable fail closed without trusting package or helper names. Descriptor proof recognizes only built-in `property` (including exact `builtins` aliases) and exact `functools.cached_property`; getter bodies use the same nested callable analysis.
- [x] Complete literal-column and official-source provenance. `column(..., is_literal=...)` is admitted only when the flag is absent or statically falsy; truthy and unknown values are rejected, with real SQLAlchemy compilation tests proving the quoted/unquoted behavior. Exact `hermes_cloud.platform.postgres.models` module aliases expose model proof. Exact SQLAlchemy `MetaData`, `Table`, `Column`, and supported Core type exports compose constructor argument provenance; unknown/external constructors, identifiers, metadata, types, columns, textual defaults, and other raw inputs remain `UNPROVEN`.
- [x] Preserve tuple and descriptor state across their real Python shapes. Multi-attribute `operator.attrgetter` returns an ordered abstract tuple of independently proven values, so unpacking, indexing, dotted paths, and subsequent invocation check every extracted sink normally. Property descriptors retain separate getter/setter/deleter slots: `.setter` and `.deleter` preserve the getter, `.getter` replaces it, and none of those mutations degrades the descriptor to an unknown value. Exact `functools.cached_property` keeps the same getter read semantics.
- [x] Reject SQLAlchemy schema/event textual execution. Exact `DDL` and the public `ExecutableDDLElement` constructor family, including locally derived subclasses, always emit DDL and raw-executable findings and never become ORM statement proof. Exact `event.listen` and `event.listens_for` reject DDL, `TextClause`, and other proven textual listeners while preserving ordinary callable listeners, including the approved SQLite PRAGMA callback. A real SQLAlchemy `MetaData.create_all` regression test proves that an attached DDL event executes during schema creation.
- [x] Complete safe inverse controls. Identity lambdas and nested lambda wrappers preserve an already proven statement. `Column` normalizes supported positional and keyword `name`/`type_` forms before validating every remaining argument. `column(..., is_literal=...)` follows static truthiness: the absent flag and statically falsy `False`, numeric zero, empty string, and `None` are non-textual; every truthy or unknown flag remains rejected.

#### Positive-proof allow/deny matrix

| Policy surface | Allow | Deny or conservatively reject |
|---|---|---|
| SQL receiver | `sqlalchemy.orm.Session`, `sqlalchemy.ext.asyncio.AsyncSession`, `sqlalchemy.Connection`/`sqlalchemy.engine.Connection`, or `sqlalchemy.ext.asyncio.AsyncConnection`, propagated through receiver annotations, constructors, exact sync/async sessionmaker factories, aliases, attributes, context entry, and local subclass MROs; the two in-repository SQLAlchemy repository bases carry an explicit `_session` summary | A receiver inferred only from a variable name, including `worker` or `logger`; a type imported from a module that does not really export it; an unknown or mixed-union receiver; an unresolved composite such as `service.database`; arbitrary imported or dynamically returned objects |
| Statement-executing API | Proven receiver calls to `execute`, `scalar`, `scalars`, `stream`, or `stream_scalars` when a statement argument is present; the same calls through a bound alias, constant-name `getattr`, or awaited async alias | The same calls or aliases on an unproven receiver; a missing or unproven statement; dynamic `getattr`; `exec_driver_sql`; SQLite cursor/connection execution outside the exact PRAGMA policy |
| Non-sink API | No-argument Result accessors such as `result.scalar()` and `result.scalars()`; methods resolved on an explicitly declared local non-SQL class instance | Treating a no-argument Result accessor as a new statement execution; treating a `worker`/`logger` name as receiver proof; allowing an unresolved composite `.execute` merely because its final attribute name looks harmless |
| Statement source | Expression values returned by `select`, `update`, `delete`, and `insert` from the exact supported SQLAlchemy modules, including SQLite dialect `insert`, and propagated through assignments, returns, and approved methods | A `Select`/`Update`/`Delete`/`Insert` annotation without a proven expression value; star-imported, unbound, non-existent-module, or foreign/local constructors; `text`; `literal_column`; strings; unknown values |
| Statement-preserving method | `where`, `join`, `select_from`, `distinct`, `order_by`, `limit`, `offset`, `values`, `execution_options`, `on_conflict_do_update`, and `on_conflict_do_nothing` | `compile`, `__str__`, `returning`, or any method outside the explicit repository allowlist |
| Function decorator | Exact built-in `staticmethod`/`classmethod` and `abc.abstractmethod`, for both sync and async functions | Unknown, external, rebound, or decorator-factory results; their decorated callable result is `UNPROVEN` even when the undecorated body returns a proven statement |
| Higher-order call | Exact standard-library `functools.partial`, built-in `getattr`, single- or multi-path `operator.attrgetter`, and `operator.methodcaller`, recursively normalized through tuples, unpacking/indexing, `__getattribute__`, and `__call__`; receiver/method and prebound arguments are checked at final invocation | Same-named external helpers; dynamic method/attribute names; an unknown wrapper receiving a SQL receiver/sink; a higher-order chain whose final SQL sink or statement cannot be proven |
| Property descriptor | Exact built-in `property`, exact aliases from `builtins`, and exact `functools.cached_property`; property getter/setter/deleter slots are tracked separately, `.setter`/`.deleter` preserve the getter, and `.getter` replaces it | Rebound or external property-like decorators; unknown getter results; descriptor mutation that loses getter provenance |
| ORM/Core argument provenance | Direct or exact-module-alias Hermes PostgreSQL models; derived model columns; exact SQLAlchemy `MetaData`, `Table`, `Column`, and supported Core types; normalized legal positional/keyword `Column` forms; ORM expressions; scalar/domain bind values; bind mappings; nested proven sequences/mappings; and identity lambda passthrough | Any unknown/raw clause or constructor argument, an external `TextClause` or predicate, textual builders, or `column(..., is_literal=...)` with a statically truthy or unknown flag; a safe constructor/method name cannot launder an unsafe argument |
| DDL and event execution | Ordinary callable SQLAlchemy event listeners, including the central SQLite PRAGMA listener; `MetaData.create_all` is not itself textual, while attached listeners are checked independently | `DDL`, public `ExecutableDDLElement` constructors or derived subclasses, DDL/TextClause event listeners through `event.listen` or `event.listens_for`, and every other proven textual schema listener |
| Typing wrapper | `Annotated[T, ...]` uses only `T`; unions of exclusively proven SQL receivers remain proven | Annotation metadata as proof; `Optional` or any union containing a non-SQL/unknown receiver; statement type annotations without a proven runtime expression |

### Task 8: Full verification

**Files:**
- Verify all changed files.

- [x] Run SQLite unit/integration/E2E tests.
- [x] Re-run the complete PostgreSQL migration suite and compare the seven publication artifacts.
- [x] Run the entire Cloud suite.
- [x] Run repo-wide Ruff check, apply full mechanical `ruff format .`, and verify repository-wide format compliance.
- [x] Build wheel and sdist, then inspect that `platform/sqlite` is in the wheel and `deploy/test_server/sqlite` is in the sdist.

Run:

```bash
cd hermes-cloud
PYTHONDONTWRITEBYTECODE=1 uv run --locked pytest -q -p no:cacheprovider
uv run --locked ruff check .
uv run --locked ruff format --check .
uv build
```

Expected: zero failures, zero lint findings, repository-wide format compliance, successful wheel/sdist build with `platform/sqlite` in the wheel and the deploy SQLite bundle in the sdist, and unchanged PostgreSQL publication evidence.

### Verification record (2026-07-31)

- AST contract scanner suite: `424 passed`.
- Production scan plus 14-module `execute`/`scalar`/`scalars` injection matrix: `43 passed`.
- SQLite/provider/deploy/E2E targeted suite: `517 passed, 71 subtests passed`.
- PostgreSQL migration V1-V7 suite: `87 passed`; all seven published checksums and structural digests matched the frozen registry.
- Full Hermes Cloud suite: `837 passed`.
- `ruff check .`: passed.
- Full mechanical `ruff format .`: `34 files reformatted, 108 files left unchanged`.
- Repository-wide `ruff format --check .`: `142 files already formatted`.
- `uv build`: wheel and sdist built successfully; wheel contains the SQLite provider and sdist contains the SQLite deployment bundle.
