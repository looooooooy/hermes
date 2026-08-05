# Hermes Cloud SQLite test-server profile

This bundle is the production-shaped SQLite profile for an Alibaba Linux host
running systemd 239. It preserves the normal Business API and Connector Gateway
entrypoints while replacing only the relational database provider.

## P0 service scope

Enable the Business API and Connector Gateway units for P0. Run the migration
and seed units explicitly before the first Business API start. The File Gateway
and Worker are not enabled in this profile; they remain outside the P0 runtime
scope.

The reviewed unit set is:

- `hermes-cloud-sqlite-business-api.service`;
- `hermes-cloud-sqlite-connector-gateway.service`;
- `hermes-cloud-sqlite-migrate.service`;
- `hermes-cloud-sqlite-seed-test-data.service`;
- `hermes-cloud-sqlite-mint-connector-token.service`.

This profile does not use `deploy/test_server/scripts/health.sh`, because that
general profile also requires File Gateway and Worker. SQLite P0 readiness is
the four explicit probes at `127.0.0.1:8101/live`,
`127.0.0.1:8101/ready`, `127.0.0.1:8102/live`, and
`127.0.0.1:8102/ready`, followed by the public HTTPS and WSS canaries.

## Accounts and shared database

Create `hermes-cloud` for runtime services and `hermes-cloud-migrate` for
migration and seed operations. Both accounts must use the `hermes-cloud` group.
The shared directory `/var/lib/hermes-cloud-sqlite` must be owned by one of
these service accounts with group `hermes-cloud` and mode `0770`. The database
file `/var/lib/hermes-cloud-sqlite/hermes-cloud.db` must use mode `0660` and
remain group-writable. A migration account in the shared group may update an
existing compliant file without taking ownership; the migration runner does
not issue a redundant `chmod` when the mode is already `0660`.

All database references must contain:

```text
sqlite+pysqlite:////var/lib/hermes-cloud-sqlite/hermes-cloud.db
```

## Secret reference contract

Provision `/etc/hermes-cloud/sqlite/secrets` as a non-symlink directory that is
not writable by untrusted users. Owner-specific references must be regular,
non-symlink files with mode `0600`. The one shared Observer keyring is the only
exception: it must be `root:hermes-cloud 0440`, so runtime and the isolated
migration account can read the same inode without either account being able to
replace it.

Ownership is service-specific:

| File | Owner | Consumer |
| --- | --- | --- |
| `runtime_database_dsn` | `hermes-cloud` | Business API and Connector Gateway |
| `business_api_signing_secret` | `hermes-cloud` | Business API |
| `connector_signing_secret` | `hermes-cloud` | Business API pairing and Connector Gateway |
| `observer_keyring.json` | `root:hermes-cloud 0440` | Business API, Connector Gateway, and Migration |
| `migration_database_dsn` | `hermes-cloud-migrate` | Migration |
| `bootstrap_database_dsn` | `hermes-cloud-migrate` | Seed |
| `initial_user_password` | `hermes-cloud-migrate` | Seed |

The keyring reader accepts only an owner-private `0600` file or the deployment-layout
`root:<effective-group> 0440` shared form. It rejects relative paths, links,
other owners/groups/modes, unsafe file replacement, oversized input, duplicate
JSON fields, malformed tenant IDs, and invalid AES-256 key material. Migration
keeps `User=hermes-cloud-migrate` and receives only the `hermes-cloud`
supplementary group. Never place key material in the environment file, service
units, output, or logs.

## Installation sequence

### Revision-11 compatibility artifact gate

Existing `dist/` artifacts must not be used for this revision-11 compatibility
fix. Before any installation or deployment, the release owner must rebuild the
wheel, sdist, legacy sdist SHA-256, SQLite release bundle, release manifest, and
standard checksum file from the current reviewed source. The only official
gate-and-build workflow is:

```text
SOURCE_DATE_EPOCH=1785628800 \
  .venv/bin/python deploy/test_server/scripts/run_release_gates.py
raw_audit_archive=/absolute/content-addressed/path/<raw-audit-id>.tar.gz
SOURCE_DATE_EPOCH=1785628800 \
  .venv/bin/python deploy/test_server/scripts/build_release.py \
  --raw-audit-archive "$raw_audit_archive"
```

It requires exactly CPython 3.12.11, canonical uv version `0.9.25`, hatchling
1.31.0, and build 1.5.0 from the frozen offline environment. Stable toolchain
identity contains only those canonical implementation/version fields. The
platform-specific `uv --version` display may be `uv 0.9.25` or may append a
strict git/date diagnostic; that raw display belongs only in the out-of-band
raw audit and must not affect stable evidence or Release ID. The build-system
requirement also pins `hatchling==1.31.0`; uv builds with `--offline
--no-build-isolation` and the verified `.venv` interpreter, so no isolated
backend resolver can select a different version. The wheel `Generator` must be
exactly `hatchling 1.31.0`.
Two independent clean project
directories and virtual environments must produce identical digests. This
bit-for-bit reproducibility is proven only for the fixed toolchain and exact
`SOURCE_DATE_EPOCH`; it is not a cross-toolchain claim.

`dist/RELEASE-MANIFEST.json` records the immutable `release_id`, all four core
artifact names, byte counts, SHA-256 digests, the wheel nested in the bundle,
the non-`dist` source-tree identity, fixed toolchain, epoch, and the digest of
the nested gate evidence set. It also records the declared required-integration
source identity from `deploy/test_server/integration-source-lock.json`. That
lock enumerates the exact Plugin, Connector, and root test-harness input files
and SHA-256 digests used only by the required cross-repository Control E2E.
Missing, extra, linked, or digest-mismatched integration inputs fail closed.
The lock digest is part of gate evidence and the Release ID description; it is
not replaced by a hash of the enclosing dirty checkout. The gate runner records each exact selection,
actual exit status, selected/passed/failed counts, normalized stdout and stderr
digests, Cloud source-tree identity, required-integration source identity,
toolchain, and deterministic epoch timestamp.
Each selection also records a raw-output SHA-256 for capture diagnostics and a
normalized-output SHA-256 for replay. Raw audit files are a separate non-core
collection; they and their raw audit archive and per-file SHA-256 digests are
excluded from `evidence_set_sha256` and the Release ID description. The
manifest identifies the one content-addressed raw audit snapshot used for the
release without turning it into a core artifact or stable gate claim. The raw audit archive is an out-of-band, non-core integrity record. It must be handed
off by an explicit absolute path, must not be placed in `dist/` or the release
directory, and is never embedded in the wheel, sdist, or SQLite bundle. An
official tool artifact SHA-256 is platform-specific acquisition evidence; an
operator must verify the digest published for the selected platform and must
not reuse a macOS artifact digest for Linux. Acquisition evidence does not
replace the canonical toolchain identity recorded by the verifier.
Normalization changes only exact pytest and unittest summary lines. Absolute
local paths are never normalized, and business output, diagnostics, and failure
text remain byte-significant. Before saving any raw stream, the runner scans for
secret assignments, authenticated proxy URLs, and absolute local paths. It must
fail closed before publication rather than redact and accidentally write
sensitive output into the audit collection or bundle.
The builder and verifier reject missing files, extra selections, failed or
unparseable output, stale source identity, toolchain drift, and digest changes.
The manifest and evidence file label this material
`attestation=untrusted/self-recorded`: it is an integrity and replay record only
and does not prove that the recorded commands ran. The bundled record is useful
for detecting mutation and reproducing the declared selections, but it is not
an independent attestation.
The enclosing checkout does not track `hermes-cloud`, so Git HEAD is explicitly
not release identity. `dist/SHA256SUMS` covers all four core artifacts and the
manifest. The manifest does not hash itself; its digest is supplied only by
`SHA256SUMS`, avoiding a self-reference cycle. The legacy
`hermes_cloud-0.1.0.tar.gz.sha256` remains for compatibility but is never the
complete release verification interface.

Before upload, use the official verifier. After upload, run the standard
checksum command in the immutable incoming directory before extracting any
archive:

```text
raw_audit_archive=/absolute/content-addressed/path/<raw-audit-id>.tar.gz
.venv/bin/python deploy/test_server/scripts/build_release.py \
  --verify-only dist --raw-audit-archive "$raw_audit_archive"
cd "/opt/hermes-cloud/incoming/$release_id"
sha256sum -c SHA256SUMS
```

On a platform without GNU `sha256sum`, the cross-platform equivalent is
`shasum -a 256 -c SHA256SUMS`. These commands pair names and digests
automatically; manual digest-to-file comparison is forbidden. Package filenames
and version `0.1.0` must never select a candidate release. Upload and candidate
release directories must be `incoming/$release_id` and `releases/$release_id`,
where `release_id` comes from the verified manifest.

Before upload, the release owner and an independent reviewer must rerun all
eight selections from the reviewed source tree with the official gate runner.
They must compare Cloud source identity, required-integration source identity,
toolchain, selection IDs, and exact counts
against the proposed release, then verify the regenerated manifest and
checksums. The independent reviewer must not accept bundled evidence as
execution proof. A later deployment or review agent repeats the same source
replay and comparison independently before authorizing deployment.
The final release embeds one self-recorded replay selected by the release owner;
the artifact does not prove two independent executions. Repeated local runs are
stability checks, while independent release verification depends on an actual
new execution from the reviewed source rather than a claim inside the bundle.

Transfer the raw audit archive separately from the six release files to a
content-addressed path keyed by its reviewed archive SHA-256. Verify that digest
before invoking the release verifier, require a regular non-symlink file owned
by the verifier with mode `0600`, and reject unsafe archive paths or unexpected
members. The archive may be deleted after verification. It is not uploaded into
the immutable release directory and does not affect the four core artifact
hashes or Release ID. This verifies integrity only; the self-recorded raw output
remains diagnostic material and is not proof that a command ran.

The release owner must also obtain all of the following fresh evidence:

- complete migration, compatibility, required cross-repository integration,
  architecture/distribution, stable Cloud, release-artifact, validation, and
  Ruff selections finish with zero failures;
- The current eight-selection baseline is 176 migration, 28 compatibility,
  1 required integration, 62 release-artifact, 92 release-validation,
  10 combined architecture/distribution, 1563 stable Cloud, and 1 Ruff result.
- The stable Cloud selection ignores only the separately required Control E2E.
  Its root contract vectors are locked Cloud-local fixtures under
  `tests/fixtures/repository_contracts`; a clean copy containing only declared
  Cloud release inputs must reproduce all 1563 stable tests without sibling
  Plugin, Connector, root-contract, or root-harness paths.
- The required integration selection must run separately against the exact
  208-file integration lock. Changing any declared sibling or harness byte must
  change its identity before a new release can be built.
- If any selection grows, the full expanded selection must still finish with
  zero failures.
- Ruff passes;
- release validation passes against the rebuilt artifacts.

Deployment is forbidden until every gate passes.

The production `.venv` does not contain pytest or Ruff. The candidate bundle
retains only the 58 standard-library `unittest` checks consumed by
`deploy/test_server/scripts/validate.sh`; complete pytest and Ruff gates run
before the manifest is accepted. The sdist is an auditable source distribution,
not the runtime deployment artifact. Only the reviewed wheel nested in the
allowlisted bundle is installed.

## Canonical revision-10 to revision-11 release runbook

This is the only command-level rev10-to-rev11 path. Do not switch `current`,
reload systemd, run cleanup, or run migration in another order. Review all
absolute paths and non-secret identifiers before executing it.

```bash
set -euo pipefail
test "$(id -u)" -eq 0 || exit 77
umask 0027

incoming_root=/opt/hermes-cloud/incoming
releases_root=/opt/hermes-cloud/releases
current_link=/opt/hermes-cloud/current
previous_link=/opt/hermes-cloud/previous
release_build_root=/opt/hermes-cloud/release-build
release_build_python="$release_build_root/.venv/bin/python"
raw_audit_root=/opt/hermes-cloud/raw-audit
profile_environment=/etc/hermes-cloud/sqlite/test-server.env
HERMES_BOOTSTRAP_DSN_FILE=/etc/hermes-cloud/sqlite/secrets/bootstrap_database_dsn
HERMES_MIGRATION_DSN_FILE=/etc/hermes-cloud/sqlite/secrets/migration_database_dsn
HERMES_RUNTIME_DSN_FILE=/etc/hermes-cloud/sqlite/secrets/runtime_database_dsn
HERMES_OBSERVER_KEYRING_FILE=/etc/hermes-cloud/sqlite/secrets/observer_keyring.json
business_unit=hermes-cloud-sqlite-business-api.service
gateway_unit=hermes-cloud-sqlite-connector-gateway.service
database=/var/lib/hermes-cloud-sqlite/hermes-cloud.db
test ! -L "$profile_environment"
test "$(stat -c '%U:%G:%a' "$profile_environment")" = root:hermes-cloud:640
test ! -L "$HERMES_RUNTIME_DSN_FILE"
test "$(stat -c '%U:%G:%a' "$HERMES_RUNTIME_DSN_FILE")" = hermes-cloud:hermes-cloud:600

# release-step-01-verify-manifest-and-candidate-venv
release_id='<release-id-from-reviewed-handoff>'
[[ "$release_id" =~ ^20[0-9]{6}T[0-9]{6}Z-[0-9a-f]{32}$ ]] || exit 78
raw_audit_id='<raw-audit-id-from-reviewed-handoff>'
raw_audit_archive_sha256='<raw-audit-archive-sha256-from-reviewed-handoff>'
[[ "$raw_audit_id" =~ ^[0-9a-f]{64}$ ]] || exit 78
[[ "$raw_audit_archive_sha256" =~ ^[0-9a-f]{64}$ ]] || exit 78
raw_audit_archive="$raw_audit_root/$raw_audit_archive_sha256/$raw_audit_id.tar.gz"
test -d "$raw_audit_root/$raw_audit_archive_sha256"
test ! -L "$raw_audit_root/$raw_audit_archive_sha256"
test -f "$raw_audit_archive"
test ! -L "$raw_audit_archive"
test "$(stat -c '%U:%G:%a' "$raw_audit_archive")" = root:root:600
test "$(sha256sum "$raw_audit_archive" | cut -d' ' -f1)" = \
  "$raw_audit_archive_sha256"
candidate_upload="$incoming_root/$release_id"
candidate_release="$releases_root/$release_id"
candidate_stage="$releases_root/.candidate-$release_id-$$"
test "$candidate_upload" = "$incoming_root/$release_id"
test "$candidate_release" = "$releases_root/$release_id"
test ! -e "$candidate_release"
test ! -L "$candidate_release"
test ! -e "$candidate_stage"
"$release_build_python" \
  "$release_build_root/deploy/test_server/scripts/build_release.py" \
  --verify-only "$candidate_upload" \
  --raw-audit-archive "$raw_audit_archive"
(cd "$candidate_upload" && sha256sum -c SHA256SUMS)
manifest_release_id=$("$release_build_python" -I -c \
  'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["release_id"])' \
  "$candidate_upload/RELEASE-MANIFEST.json")
test "$manifest_release_id" = "$release_id"
mkdir -m 0750 -- "$candidate_stage"
candidate_promoted=false
cleanup_candidate_stage() {
  if test "$candidate_promoted" = false && \
     test "$candidate_stage" = "$releases_root/.candidate-$release_id-$$" && \
     test -d "$candidate_stage" && test ! -L "$candidate_stage"; then
    rm -rf -- "$candidate_stage"
  fi
}
trap 'cleanup_candidate_stage' EXIT
tar -xzf "$candidate_upload/hermes-cloud-sqlite-release.tar.gz" \
  --strip-components=1 -C "$candidate_stage"
uv sync --project "$candidate_stage" --python /usr/bin/python3.11 \
  --frozen --no-dev --no-install-project
uv pip install --python "$candidate_stage/.venv/bin/python" --no-deps \
  "$candidate_stage/artifacts/hermes_cloud-0.1.0-py3-none-any.whl"
chown -R root:hermes-cloud -- "$candidate_stage"
chmod -R o-rwx,g+rX -- "$candidate_stage"

candidate_data_operation() {
  candidate_root=$1
  operation_purpose=$2
  operation_expectation=$3
  shift 3
  purpose_options=()
  expectation_options=()
  case "$operation_purpose" in
    validate)
      purpose_options=(
        --bootstrap-dsn-file "$HERMES_BOOTSTRAP_DSN_FILE"
        --migration-dsn-file "$HERMES_MIGRATION_DSN_FILE"
        --observer-keyring-file "$HERMES_OBSERVER_KEYRING_FILE"
        --require-seed-selectors
        --required-executable "$candidate_root/.venv/bin/python"
        --required-executable "$candidate_root/deploy/test_server/scripts/validate.sh"
        --required-executable "$candidate_root/deploy/test_server/sqlite/scripts/preflight.sh"
        --required-readable "$candidate_root/deploy/test_server/scripts/cleanup_test_seed_session.py"
        --required-readable "$candidate_root/deploy/test_server/scripts/migrate_sqlite.py"
        --required-readable "$candidate_root/deploy/test_server/sqlite/scripts/run_candidate_command.py"
      )
      ;;
    cleanup)
      purpose_options=(
        --bootstrap-dsn-file "$HERMES_BOOTSTRAP_DSN_FILE"
        --require-seed-selectors
      )
      ;;
    migration)
      purpose_options=(
        --migration-dsn-file "$HERMES_MIGRATION_DSN_FILE"
        --observer-keyring-file "$HERMES_OBSERVER_KEYRING_FILE"
      )
      ;;
    *) return 78 ;;
  esac
  if test "$operation_expectation" != none; then
    expectation_options=(--expect "$operation_expectation")
  fi
  /usr/sbin/runuser --user hermes-cloud-migrate --group hermes-cloud --supp-group hermes-cloud -- /usr/bin/env -i \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 HOME=/ TMPDIR=/tmp PYTHONNOUSERSITE=1 \
    "$candidate_root/.venv/bin/python" \
    "$candidate_root/deploy/test_server/sqlite/scripts/run_candidate_command.py" \
    --environment-file "$profile_environment" \
    --candidate-release "$candidate_root" \
    --subject migration \
    --purpose "$operation_purpose" \
    "${purpose_options[@]}" "${expectation_options[@]}" -- "$@"
}
verify_runtime_reference() {
  candidate_root=$1
  /usr/sbin/runuser --user hermes-cloud --group hermes-cloud -- /usr/bin/env -i \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 HOME=/ TMPDIR=/tmp PYTHONNOUSERSITE=1 \
    "$candidate_root/.venv/bin/python" \
    "$candidate_root/deploy/test_server/sqlite/scripts/run_candidate_command.py" \
    --environment-file "$profile_environment" \
    --candidate-release "$candidate_root" \
    --subject runtime \
    --purpose validate \
    --runtime-dsn-file "$HERMES_RUNTIME_DSN_FILE" \
    --required-executable "$candidate_root/.venv/bin/python" \
    --required-readable "$candidate_root/deploy/test_server/sqlite/scripts/run_candidate_command.py" \
    -- "$candidate_root/.venv/bin/python" -I -c 'pass'
}
run_candidate_as_migrate() {
  candidate_data_operation "$1" "$2" none "${@:3}"
}
expect_candidate_as_migrate() {
  candidate_data_operation "$1" "$2" "$3" "${@:4}"
}

run_candidate_as_migrate "$candidate_stage" validate \
  "$candidate_stage/deploy/test_server/scripts/validate.sh"
run_candidate_as_migrate "$candidate_stage" validate \
  "$candidate_stage/.venv/bin/python" -I -c \
  'import importlib.metadata as m; assert m.version("hermes-cloud") == "0.1.0"'
verify_runtime_reference "$candidate_stage"
test ! -e "$candidate_release"
mv -T -- "$candidate_stage" "$candidate_release"
candidate_promoted=true
trap - EXIT

require_runtime_stopped() {
  test "$(systemctl is-active "$business_unit" || true)" = inactive
  test "$(systemctl is-active "$gateway_unit" || true)" = inactive
}

# release-step-02-stop-business
systemctl stop "$business_unit"
# release-step-03-stop-gateway
systemctl stop "$gateway_unit"
# release-step-04-confirm-stopped-before-backup
require_runtime_stopped

# release-step-05-backup-and-verify
backup_dir="/var/backups/hermes-cloud-sqlite/$release_id"
backup="$backup_dir/hermes-cloud.db"
install -d -m 0700 "$backup_dir"
install -m 0600 "$database" "$backup"
cmp "$database" "$backup"
sha256sum "$database" "$backup"

# release-step-06-confirm-stopped-before-cleanup-plan
require_runtime_stopped
# release-step-07-cleanup-plan
run_candidate_as_migrate "$candidate_release" cleanup \
  "$candidate_release/deploy/test_server/sqlite/scripts/preflight.sh" \
  --sqlite-seed-cleanup
expect_candidate_as_migrate "$candidate_release" cleanup cleanup-plan \
  "$candidate_release/.venv/bin/python" \
  "$candidate_release/deploy/test_server/scripts/cleanup_test_seed_session.py"

# release-step-08-confirm-stopped-before-cleanup-apply
require_runtime_stopped
# release-step-09-cleanup-apply
expect_candidate_as_migrate "$candidate_release" cleanup cleanup-apply \
  "$candidate_release/.venv/bin/python" \
  "$candidate_release/deploy/test_server/scripts/cleanup_test_seed_session.py" --apply

# release-step-10-confirm-stopped-before-cleanup-absent
require_runtime_stopped
# release-step-11-cleanup-absent
expect_candidate_as_migrate "$candidate_release" cleanup cleanup-absent \
  "$candidate_release/.venv/bin/python" \
  "$candidate_release/deploy/test_server/scripts/cleanup_test_seed_session.py"

# release-step-12-confirm-stopped-before-migration-plan
require_runtime_stopped
# release-step-13-migration-plan
run_candidate_as_migrate "$candidate_release" migration \
  "$candidate_release/deploy/test_server/sqlite/scripts/preflight.sh" \
  --sqlite-migration
expect_candidate_as_migrate "$candidate_release" migration migration-plan \
  "$candidate_release/.venv/bin/python" \
  "$candidate_release/deploy/test_server/scripts/migrate_sqlite.py"

# release-step-14-confirm-stopped-before-migration-apply
require_runtime_stopped
# release-step-15-migration-apply
expect_candidate_as_migrate "$candidate_release" migration migration-apply \
  "$candidate_release/.venv/bin/python" \
  "$candidate_release/deploy/test_server/scripts/migrate_sqlite.py" --apply

# release-step-16-confirm-stopped-before-migration-current
require_runtime_stopped
# release-step-17-migration-current
expect_candidate_as_migrate "$candidate_release" migration migration-current \
  "$candidate_release/.venv/bin/python" \
  "$candidate_release/deploy/test_server/scripts/migrate_sqlite.py"

# release-step-18-atomic-current-previous-switch
old_current=$(readlink -f "$current_link")
ln -s "$old_current" "$previous_link.$release_id.next"
ln -s "$candidate_release" "$current_link.$release_id.next"
mv -Tf "$previous_link.$release_id.next" "$previous_link"
mv -Tf "$current_link.$release_id.next" "$current_link"
# release-step-19-daemon-reload
systemctl daemon-reload

# release-step-20-start-gateway
systemctl start "$gateway_unit"
# release-step-21-start-business
systemctl start "$business_unit"

# release-step-22-direct-and-public-live-ready
curl --fail --silent --show-error http://127.0.0.1:8102/live >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8102/ready >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8101/live >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8101/ready >/dev/null
curl --fail --silent --show-error https://example.invalid/hermes/live >/dev/null
curl --fail --silent --show-error https://example.invalid/hermes/ready >/dev/null

# release-step-23-wss-and-owner-control-canary
test -x "$HERMES_WSS_CANARY"
test -x "$HERMES_OWNER_CONTROL_CANARY"
"$HERMES_WSS_CANARY" --connector --observer
"$HERMES_OWNER_CONTROL_CANARY" --methods acquire,status,release
```

The root shell performs only `/opt` staging, backup, service control, and
symlink orchestration. Every candidate validation, preflight, cleanup, and
migration command crosses the data-operation boundary through explicit
`/usr/sbin/runuser --user hermes-cloud-migrate --group hermes-cloud
--supp-group hermes-cloud`, followed by absolute `/usr/bin/env -i`; no `sudo`
policy is implied. The environment file must be installed as
`root:hermes-cloud` with mode `0640` and must not be a symlink. The root shell
uses `umask 0027`; the staging root is created as `root:hermes-cloud` mode
`0750`, and its tree grants group read/traverse or execution only where the
owner execution bit is present, with no permissions for other users. The
helper refuses any EUID other
than its explicit `migration` or `runtime` subject and requires the matching
service user with primary group `hermes-cloud`. The runtime database reference
remains `hermes-cloud:hermes-cloud 0600`; only the `hermes-cloud` runtime subject
may validate it. The `hermes-cloud-migrate` subject validates the bootstrap and
migration DSNs, Observer keyring, seed selectors, and migration operations, and
is rejected if a runtime DSN option is supplied. This
prevents a root-only preflight result from being mistaken for deployability.
A non-root caller exits before any privileged staging or service command.
Before promotion, the runtime identity must read its private non-symlink DSN,
while the migrate identity must read its environment file and three
purpose-specific non-symlink DSN/keyring references. Both traverse the candidate
tree. Candidate Python and directly invoked shell entrypoints must be
executable; Python-interpreted cleanup, migration, and helper files must instead
be regular, non-symlink, readable files and need no execution bit.

The expectation wrapper captures each cleanup and migration stdout stream and
parses exactly one strict `key=value` record without shell `grep`. Duplicate,
extra, missing, malformed, or unexpected fields fail the step even when the
child exits zero. Cleanup may advance only through `plan/ready` with revision
10 and counts `1/2/1/1/1`, `apply/removed` with the same counts, then
`plan/absent` with all counts zero. Migration may advance only through a
revision-11 `versioned-10` plan, a revision-11 apply from `versioned-10`, then a
revision-11 `current` plan. An absent first cleanup plan, an empty or current
first migration plan, a wrong count, or any other result stops before symlink
replacement. Both runtime units
must remain stopped throughout backup, cleanup, and migration. Only after the
final `current` migration check may the two symlinks be atomically replaced and
systemd reloaded. Gateway always starts before Business. Replace
`example.invalid` and the two externally managed canary executables with the
reviewed deployment values; credential contents never enter the command line.

The runbook never sources the systemd `EnvironmentFile` as shell. The candidate
environment helper parses its supported assignment syntax without evaluation,
preserves reviewed values containing spaces, discards installed
`HERMES_CURRENT` and `HERMES_VENV` values, and rebinds both to the absolute
candidate. Each subject builds the child environment from a fixed allowlist containing
only the safe PATH, locale, HOME, TMPDIR, Python isolation flag, and its explicit
HERMES contract values; arbitrary parent environment variables are never
copied. The migration subject requires the reviewed nine `HERMES_SEED_*`
selectors and receives bootstrap, migration, and Observer-keyring paths. The
runtime subject receives only its runtime DSN path. Neither emits their values.
The final Release ID directory must not exist;
extraction and installation occur only in an exclusively created sibling
staging directory. Any failure removes that staging directory, while a
validated candidate is promoted to the still-absent final path with one
same-filesystem rename.

### Removing the legacy explicit test session before revision 11

An existing revision-10 test server may still contain the deterministic
`android-bootstrap` / `Hermes Cloud test session` projection. Its display name
and seed Agent name are not authoritative profile evidence, so revision 11
must not guess a durable identity for it. With both runtime services stopped
and the offline SQLite backup already verified, use only release steps 6
through 11 in the canonical runbook above. Those steps use the candidate's
absolute interpreter and cleanup runner; the old `current` release is never
used for compatibility cleanup. The dry run uses the installed, reviewed
`HERMES_SEED_*` selectors and `HERMES_BOOTSTRAP_DSN_FILE` reference. The plan
performs ORM reads only and reports the stable session identifier plus bounded
counts. It never prints the DSN, password hash, ticket digests, or message/event
payloads.

The runner requires the exact published revision-10 ORM ledger and the complete
deterministic tenant, user, role, workspace, membership, password-credential,
Agent, session, and initial-message seed fingerprint. It refuses name-only,
partial, conflicting, or ambiguous matches and refuses cleanup when an
authoritative Observer session, subscription target, deletion ledger, or event
exists for the same tenant and session key. Observer V2 state, lease, and intent rows require their parent rows
through strong foreign keys, so those parent-row checks cover them. Observer Inbox has no session dimension and therefore cannot
be safely attributed to this exact session cleanup. It preserves the tenant,
user, Agent, Device, refresh sessions, unrelated sessions and their
messages/events/cursors, and tickets not bound to `android-bootstrap`.

Dependency inspection is limited to 1,024 primary-key identities per child
table; Observer guards select only primary keys and stop after the first match.
Message content is loaded only for the one deterministic initial-message
fingerprint. The immutable revision-10 child tables already use
`tenant_id + session_id` as the leading primary-key prefix. This one-off,
offline retirement does not add an index to immutable revision 10; the
tenant-prefixed ticket and Observer checks remain hard-bounded and are not a
runtime query pattern.

After reviewing one `status=ready` plan, release step 9 applies the identical
dependency graph in one SQLAlchemy ORM transaction. Release step 11 must then
report `status=absent`.

The apply removes only the proven session row, all of its normalized
messages/events/cursors, and revision-10 tickets whose foreign session key is
`android-bootstrap`. A second plan must report `status=absent` with zero counts.
SQLite writer ownership is acquired with a mapped ORM update before the apply
reads the revision ledger, seed fingerprint, dependencies, or Observer guards.
The same transaction then revalidates that complete evidence and performs the
mapped ORM deletes, so another SQLite writer cannot insert Observer evidence in
the guard/delete window. This serializes SQLite writers only until that
transaction commits or rolls back; it does not block future writes after commit.
The requirement to keep the Business API and Connector Gateway services stopped
therefore remains mandatory for the backup, plan, apply, absent recheck, and
revision-11 migration boundary.
Only then run the revision-11 migration plan and apply. A lost commit
acknowledgement reports `cleanup outcome unknown; rerun plan`; the operator must
rerun the read-only plan instead of guessing whether to repeat a destructive
step. This is an explicit retirement operation for the old test-server seed,
not a general session deletion interface and not a runtime dependency.

The migration plan and apply result include `schema_version`, `source`, and
`recent_two_covered`. The current local schema revision is 11,
`0011_session_projection_durable_identity`. Published SQLite history contains
revisions 1 through 11; the verified historical sources are revisions 1 through
10, so current output reports `historical_source_count=10`; the recent
historical pair is `(9, 10)` and `recent_two_covered=true`. The v1-to-v11 upgrade
is therefore a published and verified path. The runner
loads the same strict keyring before opening or creating the database and passes
an AES-GCM Tenant envelope cipher into the ORM upgrader. A version-2 database
with Observer plaintext is encrypted inside the version-3 migration
transaction. Missing, unreadable, malformed, or tenant-incomplete key material
fails closed without recording version 3 or changing the plaintext rows. Empty
revision-1 data remains supported with a valid keyring.

Legacy adoption is stricter than table-name matching. SQLAlchemy Inspector
validates reflected table/column/constraint/index semantics, and a read-only
ORM projection of the SQLite 3.24-compatible `sqlite_master` catalog preserves
complete canonical table DDL plus trigger, view, and explicit-index
definitions. Existing column and table-constraint order is retained. The
bounded parser understands quoted content, nested parentheses, SQLite
block/line comments, and SQLite's exact ASCII whitespace; malformed or
ambiguous DDL fails closed.

Only the real `20260731T084500Z` unversioned release is eligible for adoption.
It must match both its frozen canonical fingerprint and an independent digest
of the exact ORM-read raw catalog rows. There is no dynamically generated
`metadata.create_all` fallback. Revision 1 uses deterministically ordered typed
Alembic operations, so newly created target DDL is stable across fresh
processes without normalizing away order from the legacy source.

The deployed v1 compatibility case is narrower than general revision-1
validation. Its ledger rows must equal the published v1 prefix, its business
objects excluding the ledger must equal both frozen `20260731T084500Z`
canonical/raw signatures, and the isolated ledger must equal both frozen v1
canonical/raw signatures. Inspector-equivalent table shape is insufficient:
raw DDL comments, extra objects, changed ledger checksums or structure, an
unknown legacy signature pair, and any attempt to use this exception at v2 or
later all fail closed.

The deployed legacy-v1-to-v5 case is independently frozen. Its ledger must be
the exact published v1-through-v5 prefix, and its complete schema, legacy base,
ledger, Observer v2/v3 overlay, Connector transport v4 overlay, and Connector
handshake v5 overlay must each match both immutable canonical and raw
signatures. A matching dry run reports `source=versioned-5-compatible`; any
extra object, component drift, checksum mutation, or use at another version
fails closed.

The deployed `20260801T131728Z` revision-10 case is also independently frozen.
Its ledger must be the exact published v1-through-v10 prefix and its complete
database must match canonical SHA-256
`f43658517c47ec0336e7e061ec4ee04aa976f3ee9d91b659a8c35720bb3944be`
plus raw-catalog SHA-256
`df2b0f97389e0844c7e0f665b2d4a3caf52b460d4b94551a74bd34ccebd54820`.
A matching dry run reports `source=versioned-10`. Release identity alone never
authorizes this path; a single field, object, raw DDL, or ledger checksum drift
fails closed.

Empty creation and legacy adoption wrap typed DDL and the ORM ledger in a
nested SQLite transaction. The ledger table is first created as a transactional
write-lock guard. On the same connection, the upgrader excludes that guard and
revalidates the planned empty source, both frozen unversioned legacy
signatures, the complete four-signature v1 compatibility proof, or the complete
multi-component v5 compatibility proof before business DDL or the ORM history
row, or the exact revision-10 ledger and complete double signature before
revision 11. It then validates the complete current state before commit; the
legacy-base current fallback still requires the exact
frozen canonical/raw ledger pair. Drift or a failed flush/commit rolls the guard and all migration
effects back. Two concurrent migration services may race, but the collision
path uses a fixed-deadline bounded polling window of read-only revalidations and
succeeds only when the other process has produced the exact current schema and
ledger.

## Historical test-server upgrade evidence

On 2026-08-02, release `20260801T131728Z` remained the active release while an
offline backup was checked read-only. Its old runner planned the backup as
`current` at revision 10. Candidate `20260801T222642Z` rejected the same backup
before migration. Read-only schema comparison proved equal table/object sets
and equal SQLAlchemy Inspector column, index, and constraint shapes; only the
preserved rev1 table-constraint order differed from the candidate's
deterministic replay, producing the exact double signature documented above.
The local revision-11 compatibility fix passes the exact fixture, drift, ledger,
and v10-to-v11 tests. No candidate artifact was built or uploaded, no live or
backup database was migrated, and no remote service was stopped, restarted,
switched, or deployed for this fix.

The 2026-07-31 test-server upgrade used `20260731T103631Z` as `current` and
kept `20260731T084500Z` as `previous`. Before switching releases, Business API
was stopped before Connector Gateway and the SQLite file was copied offline
into a private `0700` backup directory as a `root:root 0600` rollback artifact.
The source and backup sizes and SHA-256 digests matched.

The isolated backup passed the same SQLAlchemy composition and ORM migration
plan as `legacy-current`. After the release switch, the live dry run reported
`legacy-current`, the one-shot migration adopted version 1, and the next
read-only ORM plan reported `current`. Those values were emitted by the old v1
binary. The current local revision 11 candidate must instead dry-run that
deployed v1 ledger, report `source=versioned-1`, and apply the complete typed ORM
path through revision 11. Because revision 11 requires authoritative
`agent_id + profile`, the test-server seed name cannot authorize the migration;
the operator must first complete the documented external reconciliation or the
upgrade fails closed.

That dated deployed candidate passed 61 non-root deployment artifact tests, systemd
verification, nginx configuration validation, the frozen fixture digest and
mode gate, and direct/public readiness. Restarting Business API preserved the
owner-control socket inode; restarting Connector Gateway recreated the socket
with `hermes-cloud:hermes-cloud 0600` ownership and public readiness recovered.
The public canary then passed password login and refresh rotation, Session
listing, single-use Observer tickets, subscribe/unsubscribe, formal Ed25519
pairing and device authentication, Connector negotiation, control
open/acquire/status/release/close, revocation, and rejection of the old device
token. Cleanup removed 19 temporary rows through SQLAlchemy ORM; 13 exact
residue checks were all zero.

That historical run is not deployment evidence for the current local revision
11 candidate. The current stable tree adds four-part durable session identity,
stable `session_id` tickets, profile isolation, typed ORM SQLite revision 11,
and a PostgreSQL v10 non-empty-source fail-closed gate. Its final local evidence
is Cloud-local `1563 passed` and required integration `1 passed`, with
specification/quality `PASS`; no remote service was restarted, migrated,
enabled, or deployed for that candidate.

The remote rows named `android-agent`, `android-bootstrap`, and
`Hermes Cloud test session` remain deterministic test-server seed data. They are
not an Android Agent and do not prove a local Hermes runtime or authoritative
session is connected. Production `src/` has no name-based routing branch.

Include `nginx/hermes-test-server.conf` inside the existing HTTPS server block.
It exposes only the Business API, authentication, client WebSocket, Connector
WebSocket, and health routes. The existing outer `/hermes/` handler remains
unchanged.

The optional `hermes-cloud-sqlite-mint-connector-token.service` creates one
short-lived external Connector test token in its private state directory. It is
an explicit one-shot operation, has no runtime dependency, and must not be
enabled as a persistent service. It receives the private runtime database reference
and Connector signing secret by file path; neither value belongs in
the environment file, command line, output, or logs.

## Private owner-control bridge

The two runtime units use
`/run/hermes-cloud-sqlite-owner-control/owner-control.sock`. Connector Gateway
is the sole systemd `RuntimeDirectory` owner and creates the directory at mode
`0700`; Business API uses the socket but must not declare the same
`RuntimeDirectory`. Connector Gateway owns the socket at mode `0600`. This
single-owner rule ensures that restarting Business API cannot delete a live
Connector socket. It is a bounded Unix Domain Socket transport only: nginx has
no route to it, and control requests and responses are not written to SQLite.

An installation whose old Business unit also owned the runtime directory must
follow the canonical release runbook without variation. In particular, stop
Business while its old unit definition is still loaded, then stop Gateway, and
keep both stopped through the offline backup, cleanup, and migration-current
check. The single-owner unit definitions take effect only at the runbook's
symlink replacement and daemon-reload boundary. Its final direct/public health,
Connector WSS, Observer WSS, and owner-control acquire/status/release canaries
remain mandatory; readiness alone is not release evidence.

If any step fails, stop Business API before switching units or releases again.
Keep Connector Gateway stopped while restoring database state.
Restore the validated previous release with a compatible single-owner unit,
reload systemd, restart Connector Gateway, start Business API, and repeat the
socket, public WSS, and control checks. Do not treat the backed-up shared-owner
Business unit as a healthy rollback target. Database migrations must remain
expand/backward-compatible with the previous release; otherwise keep both
runtime units stopped and restore the database backup only after an explicit
operator review.

Connector negotiation may include `session.control` after the full Gateway
composition and bridge start successfully. The Business control WebSocket
advertises methods only after an authorized session resolves through ORM to
one active Agent/Device and the exact live Connector accepts the transport.

The route seed is test-server-only and enabled in the reviewed example because
the optional token oneshot preflight requires an exact production ORM binding.
Review the non-secret identifiers, then set:

```text
HERMES_SEED_OWNER_CONTROL_ENABLED=true
HERMES_SEED_TENANT_SLUG=android-test
HERMES_SEED_AGENT_KEY=android-agent
HERMES_SEED_DEVICE_KEY=android-device
HERMES_CONNECTOR_TOKEN_TENANT_ID=a495873f-cc49-5e21-b9fd-a581e3159ec8
HERMES_CONNECTOR_TOKEN_DEVICE_ID=0059b49e-fb3e-5da1-9a7c-d5a1537b2210
```

For an existing deployment or any custom seed, do not derive or guess those
UUIDs. Use the actual reviewed seed selectors to inspect the authoritative ORM
binding before changing the environment:

```bash
HERMES_SEED_OWNER_CONTROL_ENABLED=true \
HERMES_SEED_TENANT_SLUG=<reviewed-tenant-slug> \
HERMES_SEED_AGENT_KEY=<reviewed-agent-key> \
HERMES_SEED_DEVICE_KEY=<reviewed-device-key> \
HERMES_RUNTIME_DSN_FILE=/etc/hermes-cloud/sqlite/secrets/runtime_database_dsn \
/opt/hermes-cloud/current/.venv/bin/python \
  /opt/hermes-cloud/current/deploy/test_server/scripts/mint_connector_token.py \
  --inspect-binding
```

`--inspect-binding` uses the mint-specific read-only SQLAlchemy ORM
composition and the production active binding authority. It prints only non-secret UUIDs and scopes
and never prints the DSN, signing secret, or token. Do not copy the example UUIDs for a custom seed.
Use its `tenant_id` and `device_id` output, then
update `HERMES_CONNECTOR_TOKEN_TENANT_ID` and
`HERMES_CONNECTOR_TOKEN_DEVICE_ID` in the installed environment file. Retain
the reviewed custom seed slug, agent key, and device key. Then run the default dry-run before
starting the explicit mint oneshot. Only after that plan succeeds should an
operator start the apply unit.

Run `seed_test_data.py` without `--apply` first. Its plan may show one update
when upgrading an existing `android-bootstrap` projection whose `agent_id` is
empty. After review, run the seed service explicitly, then mint a new
short-lived Connector token. The seed creates only one deterministic active
Agent, binds the visible session to that Agent, and relies on the existing
active workspace membership. Owner-control opt-in additionally creates the
Device; it does not select or replace the Agent identity. The seed does not
grant wildcard permissions. These rows are test-server fixtures only. Device
pairing binds a credential to the selected `workspace_id + agent_id`; it does
not prove that a corresponding Hermes runtime, Plugin, or Local Gateway is
online, and it must not be used as Observer/Control evidence.
Token minting reads the private runtime database reference, resolves the seed
tenant slug, device key, and agent key through SQLAlchemy ORM, and requires one
active, unrevoked, unexpired, unsuspended authoritative pairing credential.
The resolved tenant and device UUIDs must exactly match the configured UUIDs.
It then emits the current V1 device-credential claim set:
`tenant_id`, `device_id`, `credential_id`, `agent_id`, `scopes`, `jti`, `iat`,
`nbf`, and `exp`. Missing, ambiguous, mismatched, revoked, expired, or suspended
state fails closed. Dry-run performs only ORM reads and never mints or writes a
token; apply writes the token atomically at mode `0600`.

The token oneshot preflight validates the absolute output and its existing
parent directory, opens the SQLite database, and executes that same production
dry-run resolution. A missing database or non-unique, revoked, expired, or
mismatched binding fails before apply without logging the secret or token.

Direct CLI legacy mode still requires canonical UUID values for both token
identifiers and retains its exact six-claim shape, but the reviewed token
oneshot preflight does not permit legacy mode. Slugs and device keys are never
written into JWT identity claims.

The local Connector must present that token with the fixed
`connector.connect` scope and must send the same tenant and device in
`connector.hello`. When the opt-in remains `false`, the seed does not create or
modify Agent/Device routing and `android-bootstrap` remains observe-only.
