# Hermes Main P0 Runtime Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use $superpower-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking via update_plan.

**Status:** Ready for inline execution on `main`. Do not create a branch or worktree.

**Goal:** Prove on the current development Mac that Hermes Desktop can authenticate to the deployed Cloud, pair the device, configure one Managed Runtime model provider through macOS Keychain, observe an explicitly staged and activated authoritative local runtime, establish authenticated Connector WSS, expose a real live Session, and complete one ordered prompt turn with a sanitized receipt.

**Architecture:** Execute an acceptance-first vertical path through Desktop, Runtime Manager, the patched Core/Plugin bundle, Connector, Cloud, and the Web gate. Desktop writes a provider credential only through the Runtime Manager macOS `SecretStore`; non-secret provider metadata points the patched Core at that Keychain reference without placing the value in argv, shell, environment, logs, artifacts, or Cloud. The managed Host runs `hermes serve` with its own `Application Support/Hermes` home; legacy `~/.hermes` is not mutated or used as the managed credential source. When a gate fails, stop at that boundary, write one failing automated regression, implement the minimum fix, rerun the live gate, and commit the verified slice directly on `main`.

**Tech Stack:** Svelte/Tauri/Rust Desktop, Rust Runtime Manager, Private CPython 3.13 with `uv`, Hermes Connector and Cloud, macOS LaunchAgents and Keychain, HTTPS/WSS, Node.js 22 real-full-chain gate, GitHub Actions.

---

## Operating constraints

- Work only in `/Users/apple/hermesmobile` on `main`.
- Do not create branches, worktrees, stashes, or speculative compatibility layers.
- Stage explicit files only. Existing generated `node_modules`, Cargo locks, `target`, and Tauri `gen` paths are not part of a commit unless a task explicitly changes the dependency policy.
- Use strict TDD for every behavior change: reproduce failure, add a failing test, implement the smallest fix, rerun the focused suite, then rerun the live gate.
- Make one local commit for each verified behavior fix. Do not batch unrelated failures.
- Do not push, deploy, reset pairing state, remove Keychain items, or change server data without explicit user authorization at that step.
- Never print or persist access tokens, refresh tokens, device private keys, approval bodies, prompt bodies, or full tool output in repository files or logs.
- A passing health endpoint or valid configuration is a prerequisite, not closure.

## Verified starting point on 2026-08-10

- Local `main` contains commit `10efb5c2` and is one commit ahead of `origin/main` at plan creation.
- The deployed Cloud release is based on `b372b9e54e3616960f58d703e5f3d6be61f98a61` and contains abandoned-pairing recovery after native Keychain identity rotation.
- Business API and Connector Gateway are active; internal and public live/ready probes return HTTP 200.
- The pairing recovery regression, Connector pairing-only suite, Desktop check/build, and Desktop Rust tests passed locally.
- The real-full-chain workflow YAML syntax fix exists locally but has not been pushed.
- Real workspace authentication, real device pairing, formal runtime readiness, authenticated Connector WSS, live Session catalog, and a real prompt receipt remain unproven.
- The legacy Hermes Agent reports configured providers, but approved migration rules forbid automatically copying or reusing those secrets. They are discovery evidence only, not P0 acceptance evidence.
- Desktop currently hard-codes DeepSeek and Kimi as `not-configured`, has no provider input, and Runtime Manager has no macOS implementation of its `SecretStore` port. The patched Core also lacks a managed Keychain-reference resolver.
- The live Runtime Manager snapshot is currently `state=absent`, with no active release or runtime generation and all four components not ready. Desktop has no runtime install/activate action, and Runtime Manager exposes staging but no initial-activation command. P0 must close this boundary explicitly; waiting for readiness cannot work.
- The latest visible macOS Managed Release qualification run at plan creation, [run 31268240913](https://github.com/looooooooy/hermes/actions/runs/31268240913), failed during immutable release assembly and produced no macOS qualification artifact. The current `main` contains later macOS Runtime Manager commits, but it has not generated a fresh accepted artifact.
- The local `origin` URL embeds a GitHub credential. Treat it as exposed: rotate it and switch `origin` to a credential-free URL before the first push. Never copy the current URL into evidence or documentation.
- `docs/staging-real-full-chain-operator-runbook.md` still pins an obsolete feature branch, PR, Issue, and candidate SHA. It must not be used as the current candidate ledger until Task 6 updates it.

## P0 closure gates

| Gate | Required result | Evidence |
| --- | --- | --- |
| G0 | Current `main` passes focused Cloud, Connector, Desktop, and Web gate tests | command output and exact Git SHA |
| G1 | Desktop OAuth completes and device pairing becomes active | sanitized Desktop state plus server-authoritative binding summary |
| G2 | Desktop stores one Managed Runtime provider in Keychain; Runtime Manager is ready; Core, Plugin, Connector, and Cloud are all ready | redacted provider metadata plus Runtime Manager JSON snapshot |
| G3 | Cloud catalog contains the paired real Agent and at least one live host-catalog Session | sanitized Agent/Session IDs and runtime generation |
| G4 | Real-full-chain workflow completes one prompt and reconnects Observer without a sequence gap | sanitized receipt artifact, workflow URL, artifact digest |
| G5 | Runbook reflects the accepted `main` candidate and all P0 evidence is reconciled | documentation commit and checklist |

Do not begin the packaged first-run/bootstrap UX, owner-action/restart, or OSS release plans until G0-G5 are all PASS.

## Execution cadence and WIP limit

- Keep exactly one gate in progress. No parallel feature work and no cleanup refactor.
- P0-A: Tasks 1-3 Steps 1-8 — local tests, initial activation, managed provider, one local commit. Target: two focused working days.
- P0-B: Task 3 Steps 9-14 — exact-SHA qualification artifact, local stage/activate, authenticated WSS. Target: one focused working day after push/install authorization.
- P0-C: Tasks 4-5 — real Agent/Session selection and one real prompt/reconnect receipt. Target: one focused working day.
- P0-D: Task 6 — reconcile the runbook and evidence, then stop P0. Target: half a working day.
- If one boundary consumes four focused hours without a green retry, stop adding code. Preserve the failing regression, record the exact blocker and next experiment, and continue only at that boundary.
- End every focused work period with either a verified local commit or an explicit failed gate; “still investigating” is not a progress state.

### Task 1: Re-establish the exact local baseline on `main`

**Files:**
- Verify: `.github/workflows/real-full-chain.yml`
- Verify: `hermes-cloud/src/hermes_cloud/platform/sqlalchemy/repositories/recoverable_device.py`
- Test: `hermes-cloud/tests/platform/sqlite/test_device_pairing_abandoned_pending_recovery.py`
- Test: `hermes-connector/tests/unit/bootstrap/test_macos_pairing_only_runtime.py`
- Test: `hermes-connector/tests/unit/bootstrap/test_cli.py`
- Test: `hermes-web/tests/real-full-chain-gate.test.mjs`
- Verify: `hermes-desktop/`

- [ ] **Step 1: Confirm the execution boundary**

Run:

```bash
cd /Users/apple/hermesmobile
test "$(git branch --show-current)" = main
git status --short --branch
git rev-parse HEAD
git worktree list
```

Expected: current branch is `main`; only `/Users/apple/hermesmobile` is listed as a worktree; generated untracked paths may remain, but no unexpected tracked modification exists.

- [ ] **Step 2: Re-run the Cloud pairing recovery gate**

Run:

```bash
cd /Users/apple/hermesmobile/hermes-cloud
uv sync --locked
uv run --locked ruff check \
  src/hermes_cloud/platform/sqlalchemy/repositories/recoverable_device.py \
  tests/platform/sqlite/test_device_pairing_abandoned_pending_recovery.py
uv run --locked pytest -q \
  tests/platform/sqlite/test_device_pairing_abandoned_pending_recovery.py
```

Expected: Ruff reports no findings and all four pairing recovery tests pass.

- [ ] **Step 3: Re-run the Connector macOS pairing-only gate**

Run:

```bash
cd /Users/apple/hermesmobile/hermes-connector
uv sync --locked
uv run --locked ruff check \
  src/hermes_connector/bootstrap/macos_pairing.py \
  src/hermes_connector/cli.py \
  tests/unit/bootstrap/test_macos_pairing_only_runtime.py \
  tests/unit/bootstrap/test_cli.py
uv run --locked pytest -q \
  tests/unit/bootstrap/test_macos_pairing_only_runtime.py \
  tests/unit/bootstrap/test_cli.py
```

Expected: Ruff reports no findings and all 22 tests pass.

- [ ] **Step 4: Re-run Desktop and real-full-chain gate checks**

Run:

```bash
cd /Users/apple/hermesmobile/hermes-desktop
npm install --no-audit --no-fund
npm run check
npm run build
cargo test --manifest-path src-tauri/Cargo.toml --lib

cd /Users/apple/hermesmobile/hermes-web
node --test tests/real-full-chain-gate.test.mjs
```

Expected: Svelte check has zero errors, Vite builds, Desktop Rust tests pass, and the real-full-chain gate test file has zero failures.

- [ ] **Step 5: Mark G0**

G0 is PASS only when every command above exits 0 on the same `main` SHA. Do not change code merely to silence unrelated warnings; record them separately.

### Task 2: Prove real Desktop OAuth and device pairing

**Files:**
- Observe first: `hermes-desktop/src/App.svelte`
- Observe first: `hermes-desktop/src/lib/Onboarding.svelte`
- Failure route, Desktop OAuth: `hermes-desktop/src-tauri/src/workspace_auth.rs`
- Failure route, Desktop session: `hermes-desktop/src-tauri/src/workspace_session.rs`
- Failure route, pairing helper: `hermes-desktop/src-tauri/src/device_pairing.rs`
- Failure route, Connector pairing: `hermes-connector/src/hermes_connector/bootstrap/macos_pairing.py`
- Failure route, Cloud conflict: `hermes-cloud/src/hermes_cloud/platform/sqlalchemy/repositories/recoverable_device.py`
- Test: `hermes-cloud/tests/platform/sqlite/test_device_pairing_abandoned_pending_recovery.py`
- Test: `hermes-connector/tests/unit/bootstrap/test_macos_pairing_only_runtime.py`
- Test: Rust unit tests colocated with the changed Desktop module

- [ ] **Step 1: Start the real Tauri application from `main`**

Run:

```bash
cd /Users/apple/hermesmobile/hermes-desktop
npm run tauri dev
```

Expected: the native Desktop window opens and obtains a Runtime Manager snapshot. A mock browser-only `npm run dev` is not acceptance.

- [ ] **Step 2: Complete the only required interactive identity action**

In the Desktop window:

1. Enter `https://api.seaotter.wiki/hermes/` as the enterprise Cloud address.
2. Select browser login.
3. Complete the real OAuth login in the browser.
4. Return to Desktop and confirm the enterprise account is shown as logged in.
5. Select **绑定这台 Mac** exactly once.
6. Wait for the operation to finish, then select **刷新状态**.

Expected: `workspaceAuthenticated=true`, `devicePaired=true`, `devicePairingState=active`, a non-empty credential fingerprint is displayed, and no HTTP 409 or pairing helper error is shown.

- [ ] **Step 3: Verify the deployed authority without changing it**

Run:

```bash
ssh -o BatchMode=yes root@8.136.200.209 '
  set -eu
  readlink -f /opt/hermes-cloud/current
  systemctl is-active hermes-cloud-sqlite-business-api.service
  systemctl is-active hermes-cloud-sqlite-connector-gateway.service
  test "$(curl -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:8101/ready)" = 200
  test "$(curl -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:8102/ready)" = 200
'
```

Expected: both units are active and both readiness checks pass. Do not dump environment files, database rows containing credential material, or unfiltered logs.

- [ ] **Step 4: Stop at the first failed boundary**

Use this routing rule:

| Symptom | First test surface | First implementation surface |
| --- | --- | --- |
| OAuth callback/session failure | Rust test in `workspace_auth.rs` or `workspace_session.rs` | the same Desktop module |
| Pairing helper exits before claim | `test_macos_pairing_only_runtime.py` plus a Rust helper test | `device_pairing.rs` or `macos_pairing.py` |
| Cloud claim returns HTTP 409 | `test_device_pairing_abandoned_pending_recovery.py` | `recoverable_device.py` |
| Pairing reaches active but Desktop remains unpaired | Rust projection test | `device_pairing.rs` projection read/write |

For the selected row: reproduce RED, implement the minimum fix, run the focused tests, rerun Steps 1-3, then use the matching exact staging command below.

OAuth callback/session failure:

```bash
git add \
  hermes-desktop/src-tauri/src/workspace_auth.rs \
  hermes-desktop/src-tauri/src/workspace_session.rs
```

Pairing helper failure:

```bash
git add \
  hermes-desktop/src-tauri/src/device_pairing.rs \
  hermes-connector/src/hermes_connector/bootstrap/macos_pairing.py \
  hermes-connector/tests/unit/bootstrap/test_macos_pairing_only_runtime.py \
  hermes-connector/tests/unit/bootstrap/test_cli.py
```

Cloud conflict failure:

```bash
git add \
  hermes-cloud/src/hermes_cloud/platform/sqlalchemy/repositories/recoverable_device.py \
  hermes-cloud/tests/platform/sqlite/test_device_pairing_abandoned_pending_recovery.py
```

Desktop projection failure:

```bash
git add hermes-desktop/src-tauri/src/device_pairing.rs
```

Finish every selected path with:

```bash
git diff --cached --check
git commit -m "fix: close real desktop pairing boundary"
```

G1 is PASS only after the real retry succeeds.

### Task 3: Close initial activation, configure the managed provider, and prove runtime readiness

**Files:**
- Modify: `.github/workflows/hermes-desktop-managed-release-payload.yml`
- Create: `hermes-runtime-manager/src/initial_activation.rs`
- Modify: `hermes-runtime-manager/src/lib.rs`
- Modify: `hermes-runtime-manager/src/main.rs`
- Modify: `hermes-runtime-manager/src/manager.rs`
- Modify: `hermes-runtime-manager/src/ports.rs`
- Modify: `hermes-runtime-manager/src/update_adapters.rs`
- Modify: `hermes-runtime-manager/src/macos_service_manager.rs`
- Create: `hermes-runtime-manager/src/macos_secret_store.rs`
- Modify: `hermes-runtime-manager/Cargo.toml`
- Create: `hermes-desktop/src-tauri/src/provider_config.rs`
- Modify: `hermes-desktop/src-tauri/Cargo.toml`
- Modify: `hermes-desktop/src-tauri/src/lib.rs`
- Modify: `hermes-desktop/src/App.svelte`
- Modify: `hermes-desktop/src/lib/Onboarding.svelte`
- Modify: `hermes-desktop/src/lib/types.ts`
- Create: `upstream/hermes-core-host-spi-v1/patches/0005-managed-provider-keychain.patch`
- Modify: `upstream/hermes-core-host-spi-v1/upstream.lock.json`
- Modify: `upstream/hermes-core-host-spi-v1/compatibility-matrix.yaml`
- Regenerate: `upstream/hermes-core-host-spi-v1/dist/hermes_agent-0.19.0-py3-none-any.whl`
- Regenerate: `upstream/hermes-core-host-spi-v1/dist/hermes_agent-0.19.0.tar.gz`
- Failure route: `hermes-connector/src/hermes_connector/bootstrap/macos.py`
- Failure route: `hermes-connector/src/hermes_connector/application/cloud_wss_client.py`
- Failure route: `hermes-connector/src/hermes_connector/adapters/cloud/websocket_transport.py`
- Test: `hermes-connector/tests/platform/macos/test_status_receipt.py`
- Test: `hermes-connector/tests/unit/application/test_cloud_wss_client.py`
- Test: `hermes-connector/tests/unit/adapters/cloud/test_websocket_transport.py`

- [ ] **Step 1: Record the real stop-the-line baseline**

Run:

```bash
cd /Users/apple/hermesmobile
cargo run --quiet --manifest-path hermes-runtime-manager/Cargo.toml -- status \
  > /tmp/hermes-runtime-manager-before-p0.json
jq -e '
  .state == "absent" and
  .active_release == null and
  .runtime_generation == null and
  ([.components[].ready] | any | not)
' /tmp/hermes-runtime-manager-before-p0.json
```

Expected at plan start: `jq` exits 0. This is evidence for the missing initial-activation slice, not a passing gate.

- [ ] **Step 2: Add failing initial-activation tests**

Create `hermes-runtime-manager/src/initial_activation.rs`. Use fakes for `ServiceManager`, readiness polling, layout, and persistent state. Add tests named `absent_manager_activates_one_exact_staged_release`, `initial_activation_rejects_an_existing_active_release`, `connector_start_failure_stops_host_and_does_not_persist_identity`, and `readiness_timeout_stops_both_services_and_does_not_claim_ready`. Assert exact call order, final lifecycle, active/previous/generation identity, and cleanup calls in each test.

In `manager.rs`, add a persistent-reload test proving that an exact active release whose four component receipts are ready reloads as `LifecycleState::Ready`; the existing empty/not-ready fake must continue to reload as `Stopped`.

In `macos_service_manager.rs`, add tests proving that Connector receives only typed non-secret endpoint/profile/path configuration. The tests must reject ambient `*_TOKEN`, `*_SECRET`, and `*_KEY` variables and reject HTTP for the API endpoint or non-WSS for the Cloud endpoint.

Add a Host LaunchAgent test proving its exact argv is `{managed hermes executable} serve`, its `HERMES_HOME` is the managed application root, and its only provider-related environment value is the non-secret absolute `HERMES_MANAGED_PROVIDER_CONFIG` reference. It must not inherit legacy `~/.hermes`, a provider key, or an OAuth token.

Run:

```bash
cd /Users/apple/hermesmobile
cargo test --manifest-path hermes-runtime-manager/Cargo.toml initial_activation
cargo test --manifest-path hermes-runtime-manager/Cargo.toml persistent_activation
cargo test --manifest-path hermes-runtime-manager/Cargo.toml launch_agent
```

Expected: RED because initial activation and typed Connector launch configuration do not exist.

- [ ] **Step 3: Implement the minimum initial-activation authority**

Implement these exact contracts:

- Add `ConnectorLaunchConfigV1` in `ports.rs` with API endpoint, WSS endpoint, display name, profile, version, application root, state directory, database file, and lock file. It must contain no credential bytes.
- Change `MacOSLaunchAgentServiceManager` to accept `Option<ConnectorLaunchConfigV1>` at construction. `start_connector` fails closed without it; status-only construction remains valid.
- Launch Host with the exact arguments `hermes serve`. Set `HERMES_HOME` to `~/Library/Application Support/Hermes`, set `HERMES_MANAGED_PROVIDER_CONFIG` to the validated profile metadata path, and keep provider secret bytes out of the LaunchAgent environment.
- Extract exact release-directory and console-script validation from `ServiceManagerReleaseActivator` into one shared helper in `update_adapters.rs`; both update activation and initial activation must reject symlinks, path escape, wrong platform layout, and non-executable entrypoints.
- Implement `InitialReleaseActivator` in `initial_activation.rs`. It accepts only an `Absent` manager, transitions `Absent → Installing`, starts Host then Connector for the exact release, waits at most 120 seconds for all four component receipts, records release ID/generation only after readiness, then transitions `Installing → Stopped → Starting → Ready`.
- If Host start, Connector start, or readiness fails, stop Connector and Host, do not persist active/previous/generation identity, transition to `Failed`, and return a generic error without child output.
- In `RuntimeManager::new_persistent`, restore `Ready` only when an active release exists and every authoritative component receipt is ready; otherwise restore `Stopped`. Never infer ready from process existence alone.
- Add the CLI form `activate-initial-release RELEASE_ID GENERATION API_ENDPOINT WSS_ENDPOINT DISPLAY_NAME` in `main.rs`. Derive local paths from `DefaultInstallLayout`, validate HTTPS/WSS and the exact staged release, then print only a sanitized `ManagerSnapshotV1`.

Run the focused tests until GREEN, then run:

```bash
cd /Users/apple/hermesmobile
cargo test --manifest-path hermes-runtime-manager/Cargo.toml
cargo build --manifest-path hermes-runtime-manager/Cargo.toml \
  --bin hermes-runtime-manager
cargo build --release --manifest-path hermes-runtime-manager/Cargo.toml \
  --bin hermes-runtime-manager
```

Expected: the complete Runtime Manager suite and release build pass.

- [ ] **Step 4: Add failing managed-provider tests**

Add tests before implementation for these contracts:

- `macos_secret_store.rs`: namespace/account validation; create/read/update/delete through a fake Security.framework adapter; redacted errors; zeroization of returned test buffers.
- `provider_config.rs`: reject unknown provider, unsafe model/base URL, short or control-character key, symlinked metadata paths, loose permissions, oversized JSON, and a Keychain/metadata partial write. A failed overwrite must restore the prior Keychain value.
- Desktop projection: `connected` is allowed only when metadata is valid, the Keychain item exists, and the exact managed Host returns `deepseek: logged in`; no secret or raw child output is serialized.
- Core patch `0005`: managed metadata overlays provider/model without mutating `os.environ`; only the declared `DEEPSEEK_API_KEY` lookup reaches the declared Keychain reference; unknown key names, unsafe metadata, missing items, and Security.framework errors fail closed and redact values.

Run the Rust tests first:

```bash
cd /Users/apple/hermesmobile
cargo test --manifest-path hermes-runtime-manager/Cargo.toml macos_secret_store
cargo test --manifest-path hermes-desktop/src-tauri/Cargo.toml --lib provider_config
```

Expected: RED because the macOS SecretStore, provider transaction, and managed Core resolver do not exist.

- [ ] **Step 5: Implement macOS Keychain and Desktop provider configuration**

Implement these exact contracts:

- `MacOSKeychainSecretStore` implements `SecretStore` with `security-framework`, fixed service namespace `com.hermes.runtime.provider.v1`, canonical account `work:deepseek`, redacted errors, and no value in `Debug`.
- Add `zeroize` to Runtime Manager and Desktop native dependencies; zeroize secret buffers after Keychain operations and tests.
- `provider_config.rs` owns `~/Library/Application Support/Hermes/profiles/work/provider-v1.json`, mode `0600`, with schema `{schema_version, provider, model, base_url, key_env, keychain_service, keychain_account}`. It contains no credential or credential digest.
- Expose Tauri commands `provider_save`, `provider_status`, and `provider_delete`. `provider_save` performs a recoverable Keychain-plus-metadata transaction; `provider_delete` requires an explicit UI action and removes metadata only after Keychain deletion is confirmed.
- P0 accepts provider `deepseek`, models `deepseek-chat|deepseek-reasoner`, optional HTTPS base URL, and key env name `DEEPSEEK_API_KEY`. Unknown fields and aliases fail closed.
- Replace `provider_slots()` with status derived from validated managed metadata, Keychain presence, and an exact managed-host auth receipt. Never inspect legacy `~/.hermes`.
- Add a real provider form to the existing Onboarding provider step. The key input is password-masked, never re-displays a saved value, and clears from Svelte state after the native call. Show only saved/tested/error state.

Run:

```bash
cd /Users/apple/hermesmobile
cargo test --manifest-path hermes-runtime-manager/Cargo.toml macos_secret_store
cargo test --manifest-path hermes-desktop/src-tauri/Cargo.toml --lib provider_config
cd hermes-desktop
npm run check
npm run build
```

Expected: focused Rust tests pass, Svelte check has zero errors, and Vite builds.

- [ ] **Step 6: Add the managed-provider Core patch and rebuild locked artifacts**

Add `0005-managed-provider-keychain.patch` after the existing four patches. It adds a bounded, no-follow Security.framework adapter and managed provider metadata reader to the pinned Core, overlays non-secret provider/model/base URL configuration, and makes credential resolution query Keychain in-process without putting the value in `os.environ`.

The patch must change only the pinned Core surfaces needed for this contract: `hermes_cli/config.py`, `hermes_cli/auth.py`, `cli.py`, the credential-pool resolver, a new `hermes_cli/managed_provider.py`, and focused Core tests. Update patch count, hashes, provenance, compatibility matrix, wheel, and sdist in `upstream.lock.json`; do not edit the generated wheel or sdist by hand.

Run:

```bash
cd /Users/apple/hermesmobile
python3 -m pytest -q \
  upstream/hermes-core-host-spi-v1/tests/test_apply_and_verify.py \
  upstream/hermes-core-host-spi-v1/tests/test_rebuild_locked_artifacts.py \
  upstream/hermes-core-host-spi-v1/tests/test_stage3_stabilization_patch_count.py
```

Expected: bundle integrity/provenance tests pass. The Host `hermes serve` contract is covered by the Runtime Manager test from Step 2, and the full pinned-Core replay remains a required job in Step 9.

- [ ] **Step 7: Make `main` capable of producing the exact macOS candidate**

In `.github/workflows/hermes-desktop-managed-release-payload.yml`, add `main` to `on.push.branches` and add `workflow_dispatch`. Keep the source lock, ephemeral qualification signature, Private Toolchain, binary-only wheelhouse, archive, local staging, and validation gates unchanged.

Run:

```bash
cd /Users/apple/hermesmobile
ruby -e 'require "yaml"; YAML.parse_file(".github/workflows/hermes-desktop-managed-release-payload.yml")'
git diff --check
```

Expected: YAML parses and the diff has no whitespace errors.

- [ ] **Step 8: Commit the locally verified activation and provider slice**

Run:

```bash
cd /Users/apple/hermesmobile
git add \
  .github/workflows/hermes-desktop-managed-release-payload.yml \
  hermes-runtime-manager/src/initial_activation.rs \
  hermes-runtime-manager/src/lib.rs \
  hermes-runtime-manager/src/main.rs \
  hermes-runtime-manager/src/manager.rs \
  hermes-runtime-manager/src/ports.rs \
  hermes-runtime-manager/src/update_adapters.rs \
  hermes-runtime-manager/src/macos_service_manager.rs \
  hermes-runtime-manager/src/macos_secret_store.rs \
  hermes-runtime-manager/Cargo.toml \
  hermes-desktop/src-tauri/src/provider_config.rs \
  hermes-desktop/src-tauri/Cargo.toml \
  hermes-desktop/src-tauri/src/lib.rs \
  hermes-desktop/src/App.svelte \
  hermes-desktop/src/lib/Onboarding.svelte \
  hermes-desktop/src/lib/types.ts \
  upstream/hermes-core-host-spi-v1/patches/0005-managed-provider-keychain.patch \
  upstream/hermes-core-host-spi-v1/upstream.lock.json \
  upstream/hermes-core-host-spi-v1/compatibility-matrix.yaml \
  upstream/hermes-core-host-spi-v1/dist/hermes_agent-0.19.0-py3-none-any.whl \
  upstream/hermes-core-host-spi-v1/dist/hermes_agent-0.19.0.tar.gz
git diff --cached --check
git commit -m "feat: close macOS initial runtime activation"
```

- [ ] **Step 9: Obtain explicit authorization, push `main`, and qualify the artifact**

This is an external mutation checkpoint. Do not push until the user explicitly authorizes it. Before authorization, the user must rotate the exposed GitHub credential and approve changing `origin` to `https://github.com/looooooooy/hermes.git`, with authentication supplied by the OS credential manager rather than the URL. After authorization, push `main`, then require the `macos-aarch64` job in `Hermes Desktop managed release payload` to pass for the exact candidate SHA. Download the artifact whose name is `hermes-managed-release-qualification-macos-aarch64-` followed by that full SHA.

Stop on any CI failure. Diagnose the first build/stage boundary, add a local regression where possible, commit one minimum fix, obtain push authorization again, and require a new exact-SHA artifact. Qualification signatures expire after six hours; perform Steps 10-12 within four hours of the successful run.

- [ ] **Step 10: Stage the exact candidate into the local immutable release root**

This changes the local Hermes installation. Obtain explicit authorization for the local installation before running it. Download the artifact ZIP to `~/Downloads`, then run:

```bash
cd /Users/apple/hermesmobile
candidate_sha="$(git rev-parse HEAD)"
artifact_zip="$HOME/Downloads/hermes-managed-release-qualification-macos-aarch64-${candidate_sha}.zip"
test -f "$artifact_zip"

p0_stage="$(mktemp -d "${TMPDIR:-/tmp}/hermes-p0-stage.XXXXXX")"
unzip -q "$artifact_zip" -d "$p0_stage/artifact"

python3 hermes-desktop/managed-release/build_managed_release_installer_zipapp.py \
  --output "$p0_stage/hermes-managed-release-installer.pyz"
python3 hermes-runtime-manager/toolchains/build_toolchain_bundle.py \
  --lock hermes-runtime-manager/toolchains/upstream-lock-v1.json \
  --target macos-aarch64 \
  --output "$p0_stage/toolchain-unqualified"
python3 hermes-runtime-manager/toolchains/qualify_toolchain_bundle.py \
  --bundle "$p0_stage/toolchain-unqualified" \
  --license-lock hermes-runtime-manager/toolchains/license-source-lock-v1.json \
  --output "$p0_stage/toolchain-qualified"

mkdir -p \
  "$HOME/Library/Application Support/Hermes/releases" \
  "$HOME/Library/Application Support/Hermes/update-stage"

hermes-runtime-manager/target/release/hermes-runtime-manager \
  stage-managed-payload \
  "$p0_stage/artifact/managed-release-macos-aarch64.tar.zst" \
  "$p0_stage/toolchain-qualified/python/bin/python3" \
  "$p0_stage/hermes-managed-release-installer.pyz" \
  "$PWD/hermes-runtime-manager/target/release/hermes-runtime-manager" \
  "$p0_stage/toolchain-qualified" \
  "$HOME/Library/Application Support/Hermes/releases" \
  "$HOME/Library/Application Support/Hermes/update-stage" \
  desktop-0.1.0-macos-aarch64 \
  1 \
  macos-aarch64
```

Expected: the receipt reports `content_verified=true`, release ID `desktop-0.1.0-macos-aarch64`, generation `1`, and an immutable release under the exact local release root. Do not delete or overwrite a different existing release.

- [ ] **Step 11: Activate the paired candidate and refresh Runtime Manager**

Run:

```bash
cd /Users/apple/hermesmobile
hermes-runtime-manager/target/release/hermes-runtime-manager \
  activate-initial-release \
  desktop-0.1.0-macos-aarch64 \
  1 \
  https://api.seaotter.wiki/hermes/ \
  wss://api.seaotter.wiki/hermes/internal/connector/ws \
  "$(scutil --get ComputerName 2>/dev/null || hostname)"

launchctl kickstart -k "gui/$(id -u)/com.hermes.runtime-manager"
```

Expected: activation returns a sanitized ready snapshot, and the restarted read-only Runtime Manager loads the same active release/generation. On failure, retain the staged release and sanitized logs; do not hand-edit the state file or LaunchAgent plist.

- [ ] **Step 12: Verify the real native projection**

Run:

```bash
cd /Users/apple/hermesmobile/hermes-desktop
npm run tauri dev
```

In the native provider step:

1. Select DeepSeek.
2. Select `deepseek-chat` or `deepseek-reasoner`.
3. Paste a real staging-safe DeepSeek key into the masked field.
4. Select **保存并验证** once.
5. Confirm the field clears and no saved value can be revealed.

Restart only the managed Host so the patched Core reloads the new non-secret metadata:

```bash
launchctl kickstart -k "gui/$(id -u)/com.hermes.runtime-manager.host"

managed_hermes="$HOME/Library/Application Support/Hermes/releases/desktop-0.1.0-macos-aarch64/host/venv/bin/hermes"
env -i \
  HOME=/Users/apple \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  HERMES_HOME="$HOME/Library/Application Support/Hermes" \
  HERMES_MANAGED_PROVIDER_CONFIG="$HOME/Library/Application Support/Hermes/profiles/work/provider-v1.json" \
  "$managed_hermes" auth status deepseek \
  | rg -x 'deepseek: logged in'
```

Select **刷新状态**. Expected: Runtime Manager connected; Core, Plugin, Connector, and Cloud healthy; Agent ready; provider `deepseek` connected; non-empty runtime generation. Confirm no API key fragment, OAuth token, auth-file path, or raw child-process output appears.

- [ ] **Step 13: Capture the authoritative Runtime Manager snapshot**

Run:

```bash
cd /Users/apple/hermesmobile
cargo run --quiet --manifest-path hermes-runtime-manager/Cargo.toml -- status \
  > /tmp/hermes-runtime-manager-p0.json
jq -e '
  .schema_version == 1 and
  .state == "ready" and
  .platform == "macos" and
  .active_release == "desktop-0.1.0-macos-aarch64" and
  .runtime_generation == "1" and
  ([.components[] | select(
    .name == "Hermes Core" or
    .name == "Agent Plugin" or
    .name == "Connector" or
    .name == "Hermes Cloud"
  )] | length == 4) and
  ([.components[].ready] | all)
' /tmp/hermes-runtime-manager-p0.json
```

Expected: `jq` exits 0. `Hermes Cloud.ready=true` must come from an exact-release Connector receipt with `cloud_state=active`, not a public HTTP probe.

- [ ] **Step 14: Route a readiness failure to one boundary**

| Snapshot result | First investigation surface |
| --- | --- |
| No active release | staging or initial-activation transaction |
| Host not running | Runtime Manager macOS LaunchAgent projection |
| Core or Plugin not ready | local descriptor and UDS handshake |
| Connector running but not ready | Connector status receipt and local authority identity |
| Connector ready but Hermes Cloud not ready | Connector WSS authentication and Cloud gateway protocol |

Add one failing test at the first failed boundary, implement one minimum fix, rerun the focused suite and Tasks 10-13, then commit on `main`. Do not weaken the provider gate or substitute a mock.

G2 is PASS only when the provider is connected in the native snapshot and the Runtime Manager assertion exits 0 for the same release/generation.

### Task 4: Select the real Agent and Session through the authenticated catalog

**Files:**
- Verify: `hermes-web/scripts/real-full-chain-gate.mjs`
- Verify: `hermes-cloud/src/hermes_cloud/platform/sqlalchemy/session_catalog.py`
- Failure route: `hermes-connector/src/hermes_connector/application/session_catalog_sync.py`
- Failure route: `hermes-connector/src/hermes_connector/adapters/platform/macos/session_catalog_client.py`
- Test: `hermes-connector/tests/platform/macos/test_session_catalog_client.py`
- Test: `hermes-connector/tests/unit/application/test_session_catalog_sync.py`
- Test: `hermes-cloud/tests/platform/sqlalchemy/test_session_catalog.py`

- [ ] **Step 1: Use the authenticated operator UI**

Read `GET /api/v1/agents` and every page of `GET /api/v1/agents/{agent_id}/sessions` through the authenticated UI. Do not copy an access token into a command or worksheet.

- [ ] **Step 2: Select one real Agent**

The Agent must be active, match the Mac paired in Task 2, use a canonical UUID, and have no `demo`, `fixture`, or `test` identity marker.

- [ ] **Step 3: Select one real primary Session**

The Session must belong to that Agent and satisfy all of:

- `directory_source=host_catalog`;
- `availability=live`;
- `is_active=true`;
- non-empty `runtime_generation`, `surface`, and `authority_revision`;
- transcript available;
- prompt/control actions advertised;
- no existing controller.

Record only Agent ID, Session ID, safe catalog fields, and runtime generation in the owner-private acceptance worksheet.

- [ ] **Step 4: Stop if the catalog is empty or stale**

If no qualifying Session exists, do not use a fixture. Add a failing session-catalog test at the first broken Connector or Cloud boundary, fix that boundary, rerun Task 3, and repeat this task.

G3 is PASS only after one unambiguous real Agent/Session pair is selected.

### Task 5: Run the real-full-chain gate on the accepted `main` candidate

**Files:**
- Verify: `.github/workflows/real-full-chain.yml`
- Verify: `hermes-web/scripts/real-full-chain-gate.mjs`
- Test: `hermes-web/tests/real-full-chain-gate.test.mjs`

- [ ] **Step 1: Freeze the candidate SHA locally**

Run:

```bash
cd /Users/apple/hermesmobile
git status --short --branch
git diff --check
git rev-parse HEAD
```

Expected: all intended behavior fixes are committed on `main`; only known generated untracked paths remain. Record the full SHA as the candidate.

- [ ] **Step 2: Obtain explicit authorization, then push `main`**

This is an external mutation checkpoint. Do not run it until the user explicitly authorizes the push.

Run after authorization:

```bash
cd /Users/apple/hermesmobile
git push origin main
git status --short --branch
```

Expected: local `main` and `origin/main` point to the same candidate SHA.

- [ ] **Step 3: Require green candidate CI**

The candidate must have successful runs for:

- Hermes Cloud pending pairing recovery;
- Cloud runtime chain;
- Hermes Connector macOS pre-runtime pairing;
- Hermes Desktop onboarding check;
- Hermes Desktop managed release payload, including full pinned-Core replay and `macos-aarch64` local staging;
- Web/Cloud/Connector/Plugin control chain.

Do not dispatch the live gate while a candidate check is pending or failed.

- [ ] **Step 4: Dispatch `Hermes real full chain`**

Use GitHub Actions with:

| Input | Value |
| --- | --- |
| `cloud_url` | `https://api.seaotter.wiki/hermes/` |
| `agent_id` | the Task 4 real Agent UUID |
| `session_id` | the Task 4 real Session UUID |
| `prompt` | one benign, unique staging acceptance sentence |
| `require_evidence` | empty |
| `timeout_ms` | `120000` |

The `hermes-runtime-staging` environment must supply `HERMES_FULL_CHAIN_ACCESS_TOKEN`. Verify only that the secret exists; never display or re-enter it in a workflow input.

- [ ] **Step 5: Verify the sanitized receipt**

Download the receipt artifact to an owner-private temporary directory and run:

```bash
jq -e '
  .schema_version == 1 and
  .gate == "hermes-real-full-chain" and
  .status == "passed" and
  .cloud_ready == true and
  .authenticated == true and
  .observer_contract == 2 and
  .control_contract == 1 and
  (.prompt_status == "accepted" or .prompt_status == "queued") and
  .assistant_stream_ordered == true and
  .assistant_terminal_event == "message.complete" and
  .reconnect_same_session == true and
  .reconnect_sequence_continuous == true
' full-chain-receipt.json

shasum -a 256 full-chain-receipt.json
```

Expected: `jq` exits 0 and a SHA-256 digest is recorded with the workflow URL, run SHA, and artifact ID. The receipt must not contain a token, prompt body, lease ID, private key, approval body, or clarification answer.

- [ ] **Step 6: Route a gate failure by its exact step**

Do not patch the gate merely because it reported a product failure.

| Gate step | Product boundary to test first |
| --- | --- |
| `catalog` | Connector catalog sync or Cloud catalog projection |
| `observer_connect` / sequence continuity | Cloud observer transport or session event ordering |
| `control_connect` / controller lease | Cloud control gateway or Connector owner-control lane |
| `prompt_submit` | Connector command lane or Plugin owner-control adapter |
| assistant terminal timeout | real Agent main loop/provider |
| reconnect continuity | replay cursor, runtime generation, or transcript projection |

For the selected boundary, use strict TDD, rerun the focused suite, rerun Tasks 3-5, and commit one fix on `main`.

G4 is PASS only after a real workflow receipt passes every assertion.

### Task 6: Reconcile the runbook and close P0 evidence

**Files:**
- Modify: `docs/staging-real-full-chain-operator-runbook.md`
- Modify: `docs/superpowers/plans/2026-08-10-hermes-main-p0-runtime-closure.md`

- [ ] **Step 1: Replace obsolete candidate metadata**

Update the runbook to describe the current `main` candidate workflow. Remove obsolete references to:

- `feature/runtime-identity-extension-health`;
- PR `#1` and Issue `#2` as current authority;
- candidate SHA `171c8cab9a42347615bb7bdbe431c018043b82d3`;
- obsolete artifact IDs and digests.

Record the accepted candidate SHA, current workflow URLs, current artifact IDs/digests, Cloud release ID, Runtime Manager active release, runtime generation, and the G0-G4 results. Do not add secrets or payload bodies.

- [ ] **Step 2: Mark plan checkboxes from evidence**

Only mark a checkbox complete when its command or live acceptance evidence exists. Leave every owner-action, restart, rollback, and OSS item outside this P0 completion claim.

- [ ] **Step 3: Verify the documentation**

Run:

```bash
cd /Users/apple/hermesmobile
if rg -n \
  'feature/runtime-identity-extension-health|171c8cab9a42347615bb7bdbe431c018043b82d3|PR #1|Issue #2' \
  docs/staging-real-full-chain-operator-runbook.md; then
  exit 1
fi
git diff --check
git diff -- docs/staging-real-full-chain-operator-runbook.md \
  docs/superpowers/plans/2026-08-10-hermes-main-p0-runtime-closure.md
```

Expected: no obsolete authority marker remains and the diff has no whitespace errors.

- [ ] **Step 4: Commit P0 closure documentation locally**

Run:

```bash
git add \
  docs/staging-real-full-chain-operator-runbook.md \
  docs/superpowers/plans/2026-08-10-hermes-main-p0-runtime-closure.md
git diff --cached --check
git commit -m "docs: record main runtime P0 closure"
```

G5 is PASS only after the documentation commit exists locally on `main` and matches the live evidence.

## P0 completion definition

P0 is closed only when G0-G5 are all PASS on one accepted `main` candidate. The completion report must contain:

- candidate SHA;
- local test counts;
- deployed Cloud release ID;
- Runtime Manager active release and runtime generation;
- real Agent and Session IDs;
- candidate CI workflow URLs;
- real-full-chain run URL, artifact ID, and receipt SHA-256;
- explicit statement that the receipt contains no secret or payload body;
- remaining non-P0 risks.

Do not claim full production closure at P0. P0 proves the real happy path and reconnect continuity only.

## Follow-on sequence after P0

Create and execute these as separate plans, in order:

1. `docs/superpowers/plans/2026-08-10-hermes-oss-release-closure.md`
   - real Alibaba Cloud OSS credentials through secret storage;
   - staging artifact upload and digest verification;
   - signed release/channel manifests;
   - Cloud update-check and download grant;
   - Desktop/Runtime Manager download, stage, activate, rollback, and evidence drill.
2. `docs/superpowers/plans/2026-08-10-hermes-packaged-first-run-closure.md`
   - install Runtime Manager, Private Toolchain, and trusted installer from the signed app package;
   - move staging/activation from operator commands into authenticated local control;
   - complete login, pairing, runtime install, provider setup, health, and ready without Terminal;
   - blank-machine install, reboot, uninstall, and legacy-discovery acceptance.
3. `docs/superpowers/plans/2026-08-10-hermes-owner-action-resilience-closure.md`
   - exact duplicate request and changed-payload conflict;
   - interrupt isolation;
   - approval and clarification matching;
   - Connector, Gateway, Business API, and Agent restart behavior;
   - runtime generation rollover, effect-unknown, backlog, log-scan, and rollback evidence.

The next skill for this plan is `$superpower-executing-plans`, executed inline on `main` with a checkpoint after each gate.
