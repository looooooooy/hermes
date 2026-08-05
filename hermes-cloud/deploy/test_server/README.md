# Hermes Cloud test server deployment artifacts

This directory is a reviewable deployment template. It does not install,
enable, restart, migrate, reload Nginx, or contact a remote host by itself.
Runtime services use the package entrypoints under
`hermes_cloud.entrypoints.*`; database migration is a separate explicit
oneshot operation. Test-data seeding is a separate dry-run-first tool and an
optional explicit apply oneshot. Connector test-token minting is also a
separate dry-run-first tool with an optional explicit apply oneshot. Runtime
services depend on neither seed nor token minting.

## Layout

- `systemd/`: four runtime services, one migration oneshot, one optional
  test-data seed oneshot, and one optional Connector token mint oneshot.
- `nginx/hermes-test-server.conf`: include-only locations for an existing TLS
  `server` block. It does not replace `nginx.conf`.
- `env/`: non-secret environment and credential-file reference examples.
- `scripts/`: bounded runners, read-only checks, and dry-run-first rollback,
  seed, legacy test-session cleanup, and Connector token helpers.
- `tests/`: static security and routing gates.

## Deployment model

Recommended filesystem layout:

```text
/opt/hermes-cloud/
├── releases/
│   ├── 20260730T120000Z/
│   └── 20260730T130000Z/
├── current  -> releases/20260730T130000Z
└── previous -> releases/20260730T120000Z
```

`HERMES_RELEASES_DIR`, `HERMES_CURRENT`, `HERMES_PREVIOUS`, and
`HERMES_VENV` are non-secret settings in `/etc/hermes-cloud/test-server.env`.
The effective process working directory is `HERMES_CURRENT`; the ASGI runner
changes to it before starting Uvicorn. Runtime services may use the release
`.venv` or a separately managed virtual environment. The release validation
gate is stricter: it always uses the physical release's own
`.venv/bin/python`.

Use two non-root accounts:

- `hermes-cloud`: four runtime services.
- `hermes-cloud-migrate`: migration only; its PostgreSQL role must differ from
  the runtime role.

The unit templates assume both accounts share the `hermes-cloud` group. The
accounts require no interactive shell and no Linux capabilities.

## Review and validation

From `hermes-cloud/`, run the default static checks:

```bash
deploy/test_server/scripts/validate.sh
```

This only runs the static tests and shell syntax checks. Optional host checks:

```bash
deploy/test_server/scripts/validate.sh --systemd
deploy/test_server/scripts/validate.sh --nginx /etc/nginx/nginx.conf
```

The optional flags only invoke `systemd-analyze verify` and `nginx -t`. They do
not reload, start, stop, enable, or modify services.

`validate.sh` resolves the physical release containing the script. When
`HERMES_RELEASES_DIR` or `HERMES_CURRENT` is present, that release must remain
inside the resolved releases directory and must be the resolved current
target. Python validation requires that exact release's executable
`.venv/bin/python` to report Python 3.11 or newer. It never discovers Python
through `PATH` and never falls back to the host system interpreter. Therefore
build the virtual environment only after the candidate is at its final
absolute release path; a missing or old release interpreter fails the gate
before any Python test is loaded.

Both the version probe and unittest discovery use Python isolated mode (`-I`)
with host Python path, home, user-base, startup, inspect, breakpoint, warning,
safe-path, and user-site environment controls removed. This prevents a host
`sitecustomize` or replacement `unittest` on `PYTHONPATH` from running inside
the gate. When `HERMES_CURRENT` is supplied, the gate resolves it again after
all Python, shell, systemd, and Nginx checks and immediately before reporting
`PASS`; a release switch during validation invalidates that run.

## Operator sequence

PostgreSQL profile only. The generic operator sequence is forbidden for a
SQLite revision-10 to revision-11 upgrade. That upgrade has exactly one
command-level path: the
[SQLite canonical runbook](sqlite/README.md#canonical-revision-10-to-revision-11-release-runbook).
In particular, its `current` symlink must not be switched before the candidate
cleanup and migration-current checks finish.

All steps below are examples for an administrator to execute deliberately.
Review paths and ownership first.

1. Create the non-root service accounts, `/opt/hermes-cloud/releases`,
   `/etc/hermes-cloud/secrets`, and the first immutable release directory.
   Build/install the locked project into the release virtual environment.

2. Point `current` to the new release. Keep `previous` pointed at the last
   known-good release. Do not delete either release during rollout.

3. Install `env/test-server.env.example` as
   `/etc/hermes-cloud/test-server.env`, then adjust only paths, loopback binds,
   ports, role identifiers, and deadline seconds.

4. Provision the runtime, migration, and test bootstrap database connection
   files, the Business API signing-secret file, the Connector signing-secret
   file, and the initial test-user password file out of band at the paths shown in
   `env/credential-files.example`. Do not paste their contents into an
   environment file, unit, command line, shell history, or repository. Each
   preflight requires an absolute, non-empty, regular non-symlink credential
   file whose permissions are no wider than `0600`; preflight never reads or
   prints its contents. The migration, bootstrap, and runtime credentials
   should use separately scoped database roles. The signing secret must
   contain at least 32 UTF-8 bytes. The Connector signing secret must contain
   32 through 4096 UTF-8 bytes.

5. Install the seven unit files under `/etc/systemd/system/`. If credential
   source paths differ, override only `LoadCredential=` in a systemd drop-in.
   Runtime units intentionally contain no dependency on migration or test-data
   seeding or Connector token minting. The seed and token-mint units have no
   `WantedBy` target and cannot start unless an operator explicitly starts
   them.

6. Include `nginx/hermes-test-server.conf` from the existing HTTPS `server`
   block that already owns `/hermes/`. The snippet routes:

   - `/hermes/api/` to the business REST process;
   - `/hermes/auth/` to the Business API authentication routes;
   - `/hermes/api/ws` to the client API WebSocket;
   - `/hermes/internal/connector/ws` to the Connector Gateway WSS process;
   - `/hermes/files/` to the file gateway;
   - `/hermes/live` and `/hermes/ready` to business health checks.

   WebSocket access logs are disabled so query tickets cannot enter access
   logs. Runtime Uvicorn processes use `--log-level warning` and
   `--no-access-log`, which also prevents INFO-level WebSocket handshake URLs
   from exposing query tickets while retaining warnings and errors. The snippet
   does not define listeners, certificates, global logs,
   `http`, `events`, or a replacement `server` block.

7. The production Connector Gateway entrypoint composes its HS256
   Bearer-token authenticator from
   `HERMES_CONNECTOR_SIGNING_SECRET_FILE`. systemd supplies that path through
   `LoadCredential=`. Missing, unsafe, or invalid signing credentials reject
   authentication and keep `/ready` at 503. Do not expose the Connector
   Gateway based on `/live` alone.

8. Run the static checks plus optional host validation. Fix every failure
   before changing host state.

9. Run migration explicitly and wait for the oneshot result:

   ```bash
   systemctl start hermes-cloud-migrate.service
   systemctl status hermes-cloud-migrate.service --no-pager
   ```

   The migration service performs its own non-root preflight and bounded
   migration run. Its preflight validates only the migration runner, the
   runner's direct imports, and the migration credential; it does not require
   Uvicorn or import runtime service entrypoints. Runtime service startup never
   invokes migrations.

   These PostgreSQL commands do not apply to SQLite. For an existing SQLite
   revision-10 installation, use only the
   [SQLite canonical runbook](sqlite/README.md#canonical-revision-10-to-revision-11-release-runbook).
   The ORM cleanup there must report `status=absent` before revision 11 is
   applied; a seed name is never accepted as Agent/profile migration evidence.

10. If the Android test account and initial transcript are required, first
    review the seed plan without writing:

    ```bash
    HERMES_SEED_TENANT_SLUG=android-test \
    HERMES_SEED_TENANT_DISPLAY_NAME="Android Test" \
    HERMES_SEED_USERNAME=android-user \
    HERMES_SEED_USER_DISPLAY_NAME="Android User" \
    HERMES_SEED_WORKSPACE_KEY=android \
    HERMES_SEED_WORKSPACE_DISPLAY_NAME=Android \
    HERMES_SEED_AGENT_KEY=<reviewed-agent-key> \
    HERMES_BOOTSTRAP_DSN_FILE=/etc/hermes-cloud/secrets/bootstrap_database_dsn \
    HERMES_INITIAL_USER_PASSWORD_FILE=/etc/hermes-cloud/secrets/initial_user_password \
    /opt/hermes-cloud/current/.venv/bin/python \
      /opt/hermes-cloud/current/deploy/test_server/scripts/seed_test_data.py
    ```

    The seven `HERMES_SEED_*` values are non-secret and must match the reviewed
    tenant, username, workspace, and Agent identifiers in `test-server.env`. The
    command reports only plan counts. It performs ORM reads in one transaction
    and makes no writes.

    After reviewing the plan, apply it explicitly:

    ```bash
    systemctl start hermes-cloud-seed-test-data.service
    systemctl status hermes-cloud-seed-test-data.service --no-pager
    ```

    The apply transaction idempotently creates or validates one tenant, active
    user, workspace role, workspace, active membership, Agent, Argon2id password
    credential, Agent-bound session projection, and assistant message. Enabling
    owner control additionally creates or validates its Device; it does not
    change the session's Agent identity. Any differing
    existing row fails closed and rolls back the whole transaction. The seed
    runner uses SQLAlchemy ORM only; it contains no raw SQL and is never a
    runtime dependency.

11. If a short-lived Connector test credential is required, first review the
    mint plan. The plan validates the same private signing credential used by
    the Gateway but does not create or print a token:

    The optional oneshot preflight requires owner control, validates the
    absolute output and its existing parent directory, opens the private
    runtime database, and runs this production dry-run resolution. It fails
    before apply when the database or exact active binding is unavailable.

    Existing owner-control installations must first resolve their actual ORM
    binding. Use the deployment's reviewed custom seed slug, device key, and
    agent key; do not infer UUIDs from this repository:

    ```bash
    HERMES_SEED_OWNER_CONTROL_ENABLED=true \
    HERMES_SEED_TENANT_SLUG=<reviewed-tenant-slug> \
    HERMES_SEED_AGENT_KEY=<reviewed-agent-key> \
    HERMES_SEED_DEVICE_KEY=<reviewed-device-key> \
    HERMES_RUNTIME_DSN_FILE=/etc/hermes-cloud/secrets/runtime_database_dsn \
    /opt/hermes-cloud/current/.venv/bin/python \
      /opt/hermes-cloud/current/deploy/test_server/scripts/mint_connector_token.py \
      --inspect-binding
    ```

    `--inspect-binding` performs only read-only SQLAlchemy ORM queries through
    the same active binding authority used by production authentication. It
    prints only non-secret UUIDs and scopes and never prints the DSN, signing secret, or token.
    Do not copy the example UUIDs for a custom seed. Copy the resolved
    `tenant_id` and `device_id` into the installed environment file. Then
    update `HERMES_CONNECTOR_TOKEN_TENANT_ID` and
    `HERMES_CONNECTOR_TOKEN_DEVICE_ID`, while retaining the reviewed custom
    seed selectors. A missing, ambiguous, expired, revoked, suspended, or
    mismatched binding is an error.

    After updating the environment, run the default dry-run before any apply:

    ```bash
    HERMES_CONNECTOR_SIGNING_SECRET_FILE=/etc/hermes-cloud/secrets/connector_signing_secret \
    HERMES_CONNECTOR_TOKEN_TENANT_ID=33333333-3333-4333-8333-333333333333 \
    HERMES_CONNECTOR_TOKEN_DEVICE_ID=77777777-7777-4777-8777-777777777777 \
    HERMES_CONNECTOR_TOKEN_TTL_SECONDS=300 \
    /opt/hermes-cloud/current/.venv/bin/python \
      /opt/hermes-cloud/current/deploy/test_server/scripts/mint_connector_token.py
    ```

    Tenant and device token identifiers are always canonical UUIDs. Direct CLI
    legacy mode preserves the six-claim token contract but rejects slugs and
    keys; the reviewed oneshot preflight does not permit legacy mode.
    When `HERMES_SEED_OWNER_CONTROL_ENABLED=true`, the runner also reads
    `HERMES_RUNTIME_DSN_FILE` from a private reference, resolves exactly one
    active ORM binding using `HERMES_SEED_TENANT_SLUG`,
    `HERMES_SEED_DEVICE_KEY`, and `HERMES_SEED_AGENT_KEY`, verifies that its
    tenant and device UUIDs equal the configured token identifiers, and emits
    the current V1 device-credential claims. Missing, ambiguous, suspended,
    revoked, expired, or mismatched bindings fail closed.

    After reviewing the plan, explicitly start the optional oneshot:

    ```bash
    systemctl start hermes-cloud-mint-connector-token.service
    systemctl status hermes-cloud-mint-connector-token.service --no-pager
    ```

    The apply operation atomically writes a `0600` token to the absolute
    `HERMES_CONNECTOR_TOKEN_OUTPUT` path and prints only completion metadata.
    It never prints the token. Legacy tokens use only their exact Connector
    claims and scope `connector.connect`; owner-control tokens use only the
    exact V1 claims and authoritative scopes. Both use HS256 and cap TTL at one
    hour. The oneshot is a test aid and is never a runtime dependency.

12. Only after migration and any deliberate test seed succeed, enable/start
    the four runtime units. Validate Nginx, then reload the existing Nginx
    service through the host's normal change procedure.

13. Check direct service readiness:

    ```bash
    deploy/test_server/scripts/health.sh live
    deploy/test_server/scripts/health.sh ready
    ```

    Then verify the same health paths through the existing public
    `/hermes/live` and `/hermes/ready` routes.

## Connector Gateway protocol boundary

The Connector Gateway exposes `/live`, `/ready`, and `/api/ws`. Nginx maps the
external `/hermes/internal/connector/ws` route to `/api/ws`.

`/api/ws` accepts only Connector Protocol v1 text frames. Each WebSocket frame
must contain exactly one UTF-8 JSON document and may not exceed 262144 bytes.
The opening request requires one valid `Authorization: Bearer ...` header, and
the first protocol frame must be `connector.hello`. The authenticated tenant
and device identifiers must exactly match the hello payload.

The runtime executes hello/welcome negotiation and bidirectional heartbeat
cursor validation. This PostgreSQL test bundle does not configure the private
owner-control bridge yet and therefore advertises only `session.observe`.
The SQLite profile documents and configures the current bounded 8101-to-8102
owner-control bridge. Without an injected durable resume resolver, only an
initial `fresh (0, 0)` proposal counts its handshake in the new epoch. A resume
or non-zero fresh proposal receives an explicit `fresh (0, 0)` new-epoch
welcome whose old-epoch handshake is not counted; the gateway does not infer
same-epoch recovery from command-router state.

## Rollback

This generic rollback helper is not an upgrade procedure. It must not be used
to reorder or replace the SQLite revision-10 to revision-11
[canonical runbook](sqlite/README.md#canonical-revision-10-to-revision-11-release-runbook).

Preview the release switch without changing state:

```bash
deploy/test_server/scripts/rollback.sh
```

After reviewing the two resolved release paths, switch only the `current` and
`previous` symlinks:

```bash
deploy/test_server/scripts/rollback.sh --apply
```

The helper never deletes a release and never restarts a service. Restart the
four runtime services explicitly, run both health checks, and validate Nginx.
Database rollback is not automatic; forward-compatible migration policy must
be handled independently.

## Logging and secrets

Uvicorn access logging is disabled and its runtime log level is `warning`, so
INFO-level WebSocket handshake URLs are suppressed while warnings and errors
remain visible. WebSocket locations also disable Nginx access logging.
Application, migration, health, validation, and rollback
helpers never print connection strings, access tokens, pairing tickets,
credential contents, or authorization headers. The migration helper reports
only the number of applied migration records. Connector token minting reports
only plan or write-completion metadata.
