# Hermes staging real full-chain operator runbook

Status: operator-ready acceptance procedure for Issue #2 and Draft PR #1.

This runbook is intentionally published on a documentation branch based on the deployment candidate. It does not advance or mutate PR #1's candidate branch. All timestamps and evidence in this runbook use UTC.

## 1. Pinned candidate and evidence reconciliation

Deployment candidate:

- Repository: looooooooy/hermes
- PR: #1
- Issue: #2
- Candidate branch: feature/runtime-identity-extension-health
- Candidate Git SHA: 171c8cab9a42347615bb7bdbe431c018043b82d3
- Pinned Hermes Core upstream SHA: 14db1a99e21e5523ee61f10f5c3300a5087e8449
- Runtime bundle: hermes-runtime-bundle-171c8cab9a42347615bb7bdbe431c018043b82d3
- Runtime bundle artifact ID: 8979361093
- Runtime bundle digest: sha256:1002729d4c639a54d17ade06c765f3e5d6b4107d0a73b2eca789067c22269229
- Cloud staging bundle: hermes-cloud-staging-171c8cab9a42347615bb7bdbe431c018043b82d3
- Cloud staging bundle artifact ID: 8979430363
- Cloud staging bundle digest: sha256:6e1bb10661776c89295124643aa17fe382e19d17024d1fdaa44f0db291a507e2
- Core wheel digest: 314593d41fd8d7673bea30310119256fee577232fd042ae4b5d005c2bdd9acea
- Core canonical sdist digest: cd29a0696834c689108fc17b82b51ad925507d7179c852a467bfafca582ad45d

Issue comments mention older candidate SHAs 37e12c6d..., 164cafa..., and 44e3ae5.... They are superseded. Do not deploy or accept one of those older candidates.

The following PR-triggered workflow runs succeeded on the exact candidate SHA:

| Workflow | Run ID | Result |
| --- | ---: | --- |
| Runtime control | 31139667163 | success |
| Plugin connector control | 31139667169 | success |
| Hermes Core Host SPI | 31139667190 | success |
| Cloud runtime chain | 31139667164 | success |

Before any live change, re-fetch PR #1. If its head SHA is no longer the candidate SHA above, stop. Reconcile the new head, rebuild the two bundles, rerun all repository gates, and replace this pin before continuing.

## 2. What is proven and what is not

Proven on the candidate:

- The four repository workflows above are green.
- The candidate bundles and their checksum manifests were built.
- A Linux single-host process stack ran Business API, Connector Gateway, File Gateway, Nginx TLS, client WSS, Connector WSS, pairing, and selected Cloud restart checks.
- Local Linux installation proved the Core wheel contains the complete hermes_state module family.
- Automated contracts cover macOS Plugin-to-Connector UDS behavior.
- The real-full-chain script fails closed and can prove a benign prompt, ordered assistant output, observer reconnect, sequence continuity, and transcript continuity.

Not yet proven in a supported staged process chain:

- A successful Deploy Hermes staging Nginx manual run on the target host.
- A successful Hermes real full chain manual run on the exact candidate against a real, non-fixture Agent and Session.
- Patched Core plus Plugin running on the supported macOS Agent host while an independent Connector process uses the production UDS endpoints.
- Real session.interrupt, approval.respond, and clarify.respond effects.
- Duplicate request id behavior through the live chain.
- Same request id with a different payload failing with 4207 and no second effect.
- Connector crash after effect start but before a durable response, producing 4307 effect_unknown instead of false success.
- Connector restart without re-executing a completed command.
- Connector Gateway restart preserving or safely invalidating pending control state.
- Business API restart without creating a second controller.
- Agent restart rotating runtime_generation and immediately invalidating old lease, transport, and Session binding.
- Target-host service-manager behavior. The reported Linux run used supervisord as PID 1 and is not systemd acceptance.
- Backlogs returning to baseline, sensitive-payload log scans, complete rollback, and two-person sign-off.

The Hermes real full chain workflow does not execute the interrupt, approval, clarification, duplicate, crash, restart, rollover, or rollback drills. A green run is necessary, not sufficient.

## 3. Roles, safety rules, and abort authority

Assign two people before starting:

- Operator: performs deployment and test actions.
- Reviewer: watches evidence, verifies identifiers and hashes, and owns the stop decision.

Use a dedicated staging tenant, Agent, Device, and Session. Do not use a customer Session or a production Agent.

Mandatory rules:

1. No password, bearer token, private key, TLS private key, model key, prompt body, approval body, clarification answer, or secret path content may be copied into GitHub inputs, issue comments, command lines, screenshots, or uploaded evidence.
2. Secret values must arrive through the configured secret manager or owner-private files. Files must be regular, non-symlinked, owned by the effective user, and mode 0600 or stricter.
3. Record lease identifiers only in the operator's private worksheet. The public evidence records that a lease existed, not its value.
4. Use UTC timestamps.
5. Do not manually rewrite a command from unknown to completed in a database.
6. Do not retry a mutation after 4307 effect_unknown. Query status only.
7. Stop immediately for candidate mismatch, checksum mismatch, ambiguous Agent or Session, existing controller, unexpected backlog growth, duplicate effect, sensitive log content, or a failed rollback preflight.
8. PR #1 stays Draft throughout the exercise.

## 4. Operator worksheet

Prepare an owner-private worksheet with these non-secret or redacted fields:

| Field | Value |
| --- | --- |
| candidate_sha | 171c8cab9a42347615bb7bdbe431c018043b82d3 |
| runtime_bundle_artifact_id | 8979361093 |
| cloud_bundle_artifact_id | 8979430363 |
| staging_change_id | operator assigned |
| cloud_release_id_current | fill |
| cloud_release_id_previous | fill |
| macos_release_id_current | fill |
| macos_release_id_previous | fill |
| cloud_service_profile | general or SQLite |
| business_api_unit | fill |
| connector_gateway_unit | fill |
| file_gateway_unit | fill |
| macos_host_launch_label | fill |
| macos_connector_launch_label | fill |
| agent_id | fill |
| session_id_primary | fill |
| session_id_canary | fill |
| runtime_generation_before | fill |
| runtime_generation_after | fill |
| operator | fill |
| reviewer | fill |
| start_utc | fill |
| end_utc | fill |

For the packaged macOS release, the expected LaunchAgent label form is:

    com.hermes.host.<release-id>
    com.hermes.connector.<release-id>

For the SQLite Cloud profile, the reviewed P0 unit names are:

    hermes-cloud-sqlite-business-api.service
    hermes-cloud-sqlite-connector-gateway.service

The current Nginx workflow also requires a process listening on loopback port 8104. The SQLite P0 document does not enable File Gateway by default. Resolve that mismatch before deployment: either use the general Cloud profile with File Gateway or explicitly deploy and review the File Gateway unit. Do not set require_backend_ready=false merely to bypass this gate.

## 5. Preflight gate

Every item must pass before deployment.

### 5.1 Git and artifacts

1. Confirm PR #1 is open and Draft.
2. Confirm the head SHA equals the pinned candidate.
3. Confirm Issue #2 remains open.
4. Confirm all four workflow run IDs in section 1 concluded success.
5. Download both bundles through the approved GitHub artifact path.
6. Verify the downloaded archive digest against GitHub.
7. Extract into a new private directory.
8. Verify every entry:

    sha256sum -c SHA256SUMS

   On macOS:

    shasum -a 256 -c SHA256SUMS

9. Confirm RELEASE.txt names the candidate SHA.
10. Confirm the Core wheel and sdist digests match section 1.
11. Stop on any mismatch. Never rebuild on the target and continue under the same candidate identity.

### 5.2 GitHub Environment

Environment name:

    hermes-runtime-staging

Required secret names:

    HERMES_FULL_CHAIN_ACCESS_TOKEN
    HERMES_STAGING_SSH_PRIVATE_KEY
    HERMES_STAGING_SSH_KNOWN_HOSTS

Verify only that each secret is configured and current. Do not reveal, print, download, or re-enter its value in workflow inputs. Require an Environment reviewer and prevent self-approval when repository policy supports it.

### 5.3 Cloud host

Verify:

- Host time synchronization is healthy.
- Nginx is installed or can be installed by the deployment script.
- TLS certificate SAN matches the intended server_name.
- Certificate and key are regular non-symlink files at reviewed absolute paths.
- Business API binds only to 127.0.0.1:8101.
- Connector Gateway binds only to 127.0.0.1:8102.
- File Gateway binds only to 127.0.0.1:8104.
- Direct probes succeed for live and ready on every deployed backend.
- Database migration is current and the exact active Agent/Device binding is unique.
- current and previous release pointers resolve to immutable release directories.
- Database backup and service-unit backup exist before any switch.
- The actual service manager and exact unit names are recorded in the worksheet.

Minimum direct checks:

    curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8101/live
    curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8101/ready

Also probe the 8102 and 8104 service-specific live and ready routes when the selected profile exposes them.

### 5.4 macOS Agent host

Verify:

- The host is macOS and is the supported production composition.
- Candidate Agent and Connector environments are separate.
- Patched Hermes Core, Plugin, and Connector were installed only from the verified runtime bundle.
- pip check passes in both environments.
- Plugin entry point is unique and enabled.
- Hermes doctor succeeds.
- Local, Control, and Observer discovery descriptors are version 2, mode 0600, owned by the effective user, and point to live UDS endpoints.
- Local, Control, and Observer descriptors have the same profile, runtime_generation, instance identity, host bundle identity, process evidence, and peer PID.
- Candidate and previous release receipts and LaunchAgent plists are available.
- The exact versioned host and Connector labels are recorded.
- The Connector is paired through the formal device flow and can authenticate without exposing token material.
- Host and Connector logs are written to private release receipt paths.
- No other Connector instance holds the process lock.

Read-only Connector validation:

    <connector-venv>/bin/python -m hermes_connector.cli --check

The formal Connector process is:

    <connector-venv>/bin/hermes-connector run --release-id <release-id>

Prefer the release activation controller and versioned LaunchAgent definitions over ad hoc terminal processes.

### 5.5 Baseline evidence

Capture before-state without payload bodies:

- Process IDs and binary digests for Business API, Connector Gateway, File Gateway, Agent, Connector, and Nginx.
- Cloud current and previous release targets.
- macOS active and previous release IDs.
- Nginx managed file digest and latest backup name.
- Agent and Device status.
- runtime_generation.
- command terminal-state counts.
- Connector owner-control executing, succeeded, failed, and unknown counts.
- pending command, inbox, outbox, and transport-journal counts.
- active controller count.
- Session message count and last event sequence for both primary and canary Sessions.
- database row counts required to detect accidental duplicate tenant, user, Agent, Device, or credential creation.

Store raw logs and database inspection output privately. Upload only sanitized summaries.

## 6. Deploy Nginx

Run the GitHub Actions workflow:

    Deploy Hermes staging Nginx

Select the candidate branch. The resulting workflow run must report GITHUB_SHA equal to the pinned candidate. If the branch moved before dispatch, stop.

Inputs:

| Input | Required value |
| --- | --- |
| host | reviewed staging SSH host; never include a password |
| ssh_user | dedicated deploy user; root only when explicitly approved |
| server_name | exact DNS name or IP present in certificate SAN |
| tls_certificate_path | reviewed absolute certificate-chain path |
| tls_certificate_key_path | reviewed absolute private-key path |
| require_backend_ready | true |

The workflow must:

- validate all inputs;
- use the pinned SSH key and known_hosts;
- render only deploy/staging/nginx/hermes-public.conf.template;
- upload the rendered site file and apply script;
- check 8101 live and ready plus TCP 8102 and 8104;
- back up only /etc/nginx/conf.d/hermes-public.conf;
- pass nginx -t;
- reload Nginx;
- pass local HTTPS /hermes/live using the installed certificate;
- automatically restore the prior managed file if a post-install step fails.

Post-deploy checks from a trusted external client:

- TLS chain and hostname validation succeed.
- HTTP redirects to HTTPS.
- /hermes/live returns success.
- /hermes/ready returns success.
- /hermes/files/ reaches the expected File Gateway behavior.
- client WebSocket ticket and hermes.tui.v1 or v2 negotiation succeed.
- Connector WSS reaches /hermes/internal/connector/ws.
- Nginx WebSocket access logging remains disabled.
- No backend port is publicly reachable.

Record workflow URL, run ID, run SHA, start/end UTC, managed-config SHA-256, backup filename, and the result of every probe. Do not record SSH or TLS secret material.

## 7. Select the live Agent and Sessions

Use the authenticated operator UI or an approved client that reads authorization from an owner-private file. Never put a bearer token in a command argument or shell history.

Resolve Agents from:

    GET <cloud_url>/api/v1/agents

The selected Agent must:

- match an explicit canonical UUID;
- have status active;
- be the real paired macOS Agent;
- not have demo, fixture, or test in its authoritative key.

Resolve all pages of Sessions from:

    GET <cloud_url>/api/v1/agents/<agent_id>/sessions?min_messages=0&archived=exclude&order=recent&limit=50&offset=<offset>

Select a primary Session and a second canary Session. Each must:

- belong to the selected Agent;
- have directory_source equal to host_catalog;
- have availability equal to live;
- have is_active equal to true;
- have a non-empty runtime_generation;
- have a non-empty surface and authority_revision;
- have transcript_available enabled where required;
- advertise every action needed for its drill;
- not be a demo, fixture, or test identity;
- be safe to mutate with benign acceptance actions.

Reject an ambiguous catalog, duplicate Session ID, duplicate session key, moving total during pagination, or a Session not owned by the Agent.

Before acquiring control, session.control.status for the primary Session must report controller_kind none. If another controller exists, stop; do not evict it for acceptance.

Record the Agent UUID, primary and canary Session UUIDs, profile, surface, authority revision, runtime generation, available action names, and baseline sequence/message counts. Do not record transcript content.

## 8. Run the automated real-full-chain baseline

Run the GitHub Actions workflow:

    Hermes real full chain

The run must use the candidate branch and report the pinned candidate SHA.

Inputs:

| Input | Value |
| --- | --- |
| cloud_url | HTTPS base ending at the public /hermes/ prefix expected by the API |
| agent_id | selected real Agent UUID |
| session_id | selected real primary Session UUID |
| prompt | benign unique acceptance prompt with no secret or customer content |
| require_evidence | empty for the baseline; use only for a deliberately prepared scene |
| timeout_ms | 120000 initially; allowed range is 5000 through 300000 |

The script performs, in order:

1. exact Cloud ready check;
2. authenticated Agent and complete paginated Session catalog validation;
3. observer-v2 ticket and subscription;
4. control-v1 ticket and capability validation;
5. session.control.status and no-existing-controller check;
6. session.control.acquire;
7. status reconciliation against the lease;
8. one prompt.submit with generated client request and turn IDs;
9. ordered message.start, message.delta, and message.complete observation;
10. post-prompt status;
11. Observer disconnect and reconnect to the same Session;
12. runtime generation, event sequence, replay digest, and transcript continuity checks;
13. session.control.release;
14. sanitized receipt upload.

A passing receipt must contain:

- schema_version 1;
- gate hermes-real-full-chain;
- status passed;
- cloud_ready true;
- authenticated true;
- exact Agent and Session IDs;
- observer_contract 2;
- control_contract 1;
- prompt_status accepted or queued;
- assistant_stream_ordered true;
- assistant_terminal_event message.complete;
- reconnect_same_session true;
- reconnect_sequence_continuous true.

Download the receipt artifact. Compute and record its SHA-256 and artifact ID. Verify that it contains no token, prompt body, lease ID, approval body, or clarification answer.

Important limitation: require_evidence observes evidence classes; it does not respond to a pending approval or clarification. Leave it empty for the baseline. Use it only when a second approved control surface resolves the deliberately prepared pending input before the workflow deadline.

## 9. Live owner-action and idempotency drills

Use the production Web or Android control client, or a reviewed temporary operator client built from the same CloudCommandPort contract. Do not modify production databases or synthesize receipts.

For every mutation:

- keep the same authenticated principal, client instance, Session, and live lease;
- generate one UUID client_request_id;
- record method, client_request_id, terminal status, event-sequence range, and timestamps;
- do not record the payload body;
- verify the actual Session effect through Observer and transcript/state counts.

The common mutation scope is:

    session_id
    lease_id
    client_request_id

Keep lease_id private.

### 9.1 Prompt and duplicate behavior

1. Submit one benign prompt with a fixed client_request_id and client_turn_id.
2. Wait for the real assistant terminal event and transcript projection.
3. Repeat the exact same method, request ID, turn ID, and canonical payload.
4. Expected: the prior result is returned; no second user turn, assistant turn, tool action, or event-sequence effect appears.
5. Reuse the same client_request_id with a different benign prompt payload.
6. Expected: error 4207 request_id_payload_conflict; no second effect.
7. Query session.command.status using session_id, method prompt.submit, and the original client_request_id.
8. Expected: generic accepted, queued, or rejected projection consistent with the observed effect.

The automated workflow cannot perform this drill because it generates a new request ID internally.

### 9.2 Interrupt isolation

1. Confirm the canary Session is idle and capture its sequence/message counts.
2. Start a benign long-running turn in the primary Session.
3. Confirm Observer shows that primary turn running.
4. Send session.interrupt to the primary Session with a new client_request_id.
5. Expected: accepted result, the primary execution becomes interrupted, and no later effect from that turn continues.
6. Re-read the canary Session.
7. Expected: its controller state, running state, sequence, and transcript counts are unchanged.
8. Repeat the same interrupt request exactly.
9. Expected: prior result and no second effect.
10. Reuse the ID with a different canonical scope or payload.
11. Expected: 4207 and no effect.

### 9.3 Approval response

Prepare a staging-only operation that creates a real approval without touching customer data. A suitable scene is a reversible write to a dedicated staging canary path governed by the normal approval engine.

1. Observe pending_input kind approval through the control snapshot.
2. Privately verify its server request_id and server-provided choices.
3. Choose only a value present in choices. Use deny or allow_once unless the test specifically requires another reviewed choice.
4. Send approval.respond with the common scope plus request_id and choice.
5. Expected: accepted, kind approval, exact request_id and client_request_id, and a higher control_revision.
6. Verify only that exact pending approval is resolved and that another Session's pending input is unchanged.
7. Repeat the same response with the same client_request_id.
8. Expected: prior accepted result and no second effect.
9. Reuse the ID with a different choice.
10. Expected: 4207.
11. Use a new client_request_id against an expired, resolved, or superseded request.
12. Expected: 4208.
13. Use a choice not present in the authoritative snapshot.
14. Expected: 4213.

### 9.4 Clarification response

Prepare a staging-only action that creates a real clarification.

1. Observe pending_input kind clarify.
2. Privately verify request_id, authoritative choices, and allow_other.
3. Send clarify.respond with exactly one answer form: request_id plus choice_id, or request_id plus non-blank other_text only when allow_other is true.
4. Expected: accepted, kind clarify, exact request_id and client_request_id, and a higher control_revision.
5. Verify only the matching clarification is resolved.
6. Repeat the same response with the same client_request_id.
7. Expected: prior accepted result and no second effect.
8. Reuse the ID with different answer content.
9. Expected: 4207.
10. Use a new ID for an expired or already resolved request.
11. Expected: 4208.
12. Send an unauthorized choice or answer form.
13. Expected: 4213.

## 10. Restart, crash, and rollover drills

Run one drill at a time. Return health and backlogs to baseline before starting the next.

### 10.1 Connector graceful restart

1. Finish and record a completed prompt command.
2. Capture its request ID, terminal status, transcript counts, and Connector ledger counts.
3. Stop the exact versioned Connector LaunchAgent gracefully.
4. Confirm Cloud health marks the Connector unavailable within the reviewed deadline.
5. Start the same versioned Connector LaunchAgent.
6. Confirm local UDS reattachment, Cloud WSS welcome, and health recovery.
7. Query the original command status.
8. Repeat the exact original mutation.
9. Expected: the completed command is not executed again and any unsent durable response is replayed with the same identity.
10. Confirm inbox, outbox, owner-control, and transport-journal backlogs return to baseline.

Use the deployment's recorded GUI domain and exact versioned label. Typical inspection and restart operations are:

    launchctl print gui/<uid>/<connector-label>
    launchctl kickstart -k gui/<uid>/<connector-label>

Do not guess the label or use an unversioned service.

### 10.2 effect_unknown crash window

This drill requires a deterministic, reviewed staging fault mechanism. The required phase is after the Connector has started the local owner mutation but before the Plugin response has been durably completed in the Connector owner-control record.

The current repository has no operator-facing deterministic failpoint for that exact phase. A random process kill is not acceptable evidence because it cannot prove whether the effect had started. Before executing this drill, the operator must have one of:

- a reviewed test-only candidate fault hook that pauses at the exact phase and is absent from production mode; or
- an external trace-controlled procedure that proves the local mutation write began and the response was not durably recorded before termination.

Procedure:

1. Use a benign effect whose occurrence can be counted without exposing its payload.
2. Start the mutation with a fixed client_request_id.
3. Capture proof that the effect-start boundary was crossed.
4. Terminate the Connector before durable response completion.
5. Expected Cloud result: 4307 effect_unknown, never accepted or completed.
6. Query session.command.status with the original method and request ID.
7. If a generic terminal status is available, reconcile it without resend.
8. If status returns 4210 command_unknown, preserve unknown and do not resend.
9. Restart Connector.
10. Verify the executing owner-control record becomes or remains unknown, no duplicate effect occurs, and the backlog returns to baseline.
11. Remove or disable the fault mechanism before further testing.

If the exact phase cannot be proven, mark this criterion failed and keep PR #1 Draft.

### 10.3 Connector Gateway restart

1. Capture pending control state, controller revision, Connector identity, and backlog counts.
2. Start one controlled request and note whether effect_started is false or true.
3. Restart the exact Connector Gateway service.
4. Expected: every pending request is either preserved and reconciled, failed definitively before effect, or returned as effect_unknown after effect start. No request may become false success.
5. Confirm Connector WSS reconnects with a valid new epoch or valid resume accepted by the protocol.
6. Confirm old transport state cannot authorize a mutation after invalidation.
7. Query, never blindly resend, an unknown request.
8. Confirm health and backlogs return to baseline.

For the SQLite profile, use the recorded unit:

    systemctl restart hermes-cloud-sqlite-connector-gateway.service

For another profile, use only the reviewed recorded unit name.

### 10.4 Business API restart

1. Acquire one controller and record only its existence and control_revision.
2. Restart the exact Business API service.
3. Expected: client sockets close and reconnect cleanly; the restart does not create a second controller.
4. The old lease must not silently authorize a new transport.
5. Reconcile session.control.status.
6. Re-acquire only after status shows no live conflicting controller or the old lease has safely expired.
7. Verify Connector Gateway socket ownership and inode behavior remains correct for the deployed profile.
8. Confirm Agent, Device, credential, and Session row counts are unchanged.
9. Confirm health, WSS, and backlogs return to baseline.

For the SQLite profile:

    systemctl restart hermes-cloud-sqlite-business-api.service

### 10.5 Agent restart and runtime_generation rollover

1. Record runtime_generation G0 from the authoritative catalog and Observer subscription.
2. Keep an old control lease and old Observer/control transports only for negative checks; never publish their values.
3. Gracefully restart the exact versioned Hermes Host LaunchAgent.
4. Record process stop/start UTC and new PID evidence.
5. Wait for Plugin descriptors to republish and Connector to reattach.
6. Fetch the authoritative catalog and Observer subscription again.
7. Expected: runtime_generation G1 is non-empty and G1 differs from G0.
8. Expected: old lease, old transport, and old Session binding fail immediately with the appropriate closed or mismatch error; none may mutate the new runtime.
9. Expected: sequence origin and replay behavior follow the new generation contract; an old-generation resume is rejected.
10. Acquire a new lease and run one benign prompt on G1.
11. Confirm exactly one effect and healthy transcript continuity within G1.
12. Confirm all backlogs return to baseline.

Typical versioned host restart:

    launchctl print gui/<uid>/<host-label>
    launchctl kickstart -k gui/<uid>/<host-label>

## 11. Log and backlog verification

Create an owner-private pattern file containing the exact benign prompt, approval display/body text, clarification text, token fingerprint material, and any other values that must never appear. Do not upload that file.

Scan raw logs by file name only:

    rg -l -F -f <private-pattern-file> <private-log-root>

Expected result: no matching file.

Also inspect Nginx, Business API, Connector Gateway, File Gateway, Agent, Plugin, and Connector configuration for:

- access logging disabled on both WebSocket paths;
- no Authorization or ticket query values;
- no lease IDs;
- no prompt, approval, or clarification bodies;
- no credential or private secret-file contents;
- no unsafe absolute secret path content in errors.

Public evidence should contain only:

- scanner version and configuration digest;
- scanned file count and byte count;
- zero-match result;
- UTC start/end;
- reviewer sign-off.

After every drill, compare against the baseline:

- pending command count;
- Connector inbox and outbox;
- owner-control executing and unknown counts;
- durable transport journal;
- control transports and active controller count;
- process, task, file descriptor, and UDS endpoint counts where available.

The final state must return to baseline or have a reviewed, explained delta. An unexplained positive delta fails acceptance.

## 12. Rollback drill

Rollback must cover Cloud, Nginx, Agent, and Connector. Do not wait for a real failure.

### 12.1 Nginx

1. Record the pre-change managed-file SHA-256 and backup filename.
2. Prove automatic rollback in staging with a reviewed invalid candidate that reaches nginx -t after backup creation.
3. Expected: apply_nginx.sh restores the old managed file, leaves nginx -t valid, and does not expose a partial config.
4. Then perform the normal manual rollback using the verified backup:

    cp <verified-backup> /etc/nginx/conf.d/hermes-public.conf
    nginx -t
    systemctl reload nginx

5. Verify HTTPS health, client WSS, Connector WSS, and the restored file digest.

### 12.2 Cloud

1. Stop acceptance command ingress.
2. Stop Business API before Connector Gateway when database or owner-control socket changes require it.
3. Run the committed rollback helper in preview mode.
4. Verify the resolved current and previous immutable release paths.
5. Apply the release-pointer rollback:

    deploy/test_server/scripts/rollback.sh --apply

6. Restart the reviewed Cloud runtime units explicitly.
7. Run direct and public live/ready checks, client WSS, Connector WSS, and owner-control acquire/status/release.
8. Confirm database compatibility. Database rollback is never automatic; restore a backup only under the profile-specific migration runbook and reviewer approval.

### 12.3 macOS Agent and Connector

1. Stop new Cloud mutation ingress.
2. Stop the candidate Connector.
3. Preserve every unconfirmed request as effect_unknown.
4. Stop the candidate Host.
5. Use the activation controller's previous release receipt and validated backup plists.
6. Restore the old Host service first, then the old Connector service.
7. Verify the previous release binary and plist digests.
8. Start old Host and wait for its descriptors.
9. Start old Connector and wait for local UDS plus Cloud WSS readiness.
10. Verify only commands bound to the current restored runtime generation are accepted.
11. Run one benign prompt, Observer reconnect, and transcript continuity check.
12. Confirm backlogs and process resources return to the rollback baseline.

Never hand-edit the database to convert unknown into success.

### 12.4 Roll forward

After the rollback proof is accepted, either leave the previous release active or repeat the entire candidate deployment preflight and switch. Do not silently switch back without recording a second change window and health evidence.

## 13. Evidence package

Create one private evidence directory:

    evidence/staging/<UTC-start>-171c8cab/

Required sanitized files:

| File | Required content |
| --- | --- |
| manifest.json | candidate SHA, artifact IDs and digests, tool versions, operator/reviewer, UTC window |
| preflight.json | pass/fail for every preflight item |
| deployment.json | workflow URLs/IDs/SHAs, service release IDs, config digests, health results |
| catalog.json | Agent/Session IDs, safe catalog fields, G0/G1; no transcript bodies |
| full-chain-receipt.json | exact sanitized workflow receipt |
| commands.jsonl | method, client_request_id, pending request ID where safe, terminal state, error code, event range, UTC |
| restarts.jsonl | component, old/new PID or generation, stop/start/ready UTC |
| backlog-before.json | baseline counts |
| backlog-after.json | final counts and reviewed deltas |
| log-scan.json | file/byte counts, patterns-file digest, zero-match result; no patterns |
| rollback.json | old/new release IDs, config digests, health and canary outcomes |
| acceptance.md | checklist and two-person sign-off |

Compute a checksum manifest:

    find evidence/staging/<run-dir> -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > evidence/staging/<run-dir>/SHA256SUMS
    sha256sum -c evidence/staging/<run-dir>/SHA256SUMS

Upload only the sanitized package through the approved evidence path. Record its artifact ID, digest, and retention date in Issue #2. Do not commit live evidence or secrets to the repository.

## 14. Exact acceptance criteria for moving PR #1 out of Draft

Every row must be PASS. No waiver or "covered by unit tests" substitute is allowed.

| ID | Criterion | Minimum evidence |
| --- | --- | --- |
| A1 | PR head equals the accepted candidate | PR URL and full SHA |
| A2 | All four repository gates succeed on that SHA | workflow URLs and run IDs |
| A3 | Runtime and Cloud bundle digests match section 1 | artifact IDs plus SHA-256 and verified SHA256SUMS |
| D1 | Target Cloud/Nginx deployment is healthy | Nginx run URL/SHA, direct/public health, TLS and WSS canaries |
| D2 | Supported macOS Host runs patched Core and Plugin; independent Connector uses real UDS | release IDs, process evidence, descriptor/peer binding summary |
| C1 | Explicit active Agent and live host-catalog primary/canary Sessions are selected | sanitized catalog evidence |
| F1 | Automated real-full-chain baseline passes | workflow URL, run SHA, receipt artifact ID and digest |
| F2 | Prompt reaches real main loop; ordered message.complete and reconnect continuity pass | receipt plus Observer event ranges |
| I1 | Exact duplicate request returns prior result with one effect | command ID, status, transcript/event count delta |
| I2 | Same request ID with changed payload returns 4207 with no second effect | command ID, error code, unchanged effect count |
| O1 | Interrupt affects only the selected Session | primary and canary before/after counts |
| O2 | Approval resolves only the matching pending request | request/command IDs, control revision, negative 4208/4213 checks |
| O3 | Clarification resolves only the matching pending request | request/command IDs, control revision, negative 4208/4213 checks |
| R1 | Connector graceful restart does not replay a completed effect | restart timestamps, command status, unchanged effect count |
| R2 | Effect-start crash before durable response returns 4307 effect_unknown and is never auto-resent | deterministic phase proof, command state, status query, restart result |
| R3 | Connector Gateway restart preserves or safely invalidates pending state | pending-state before/after and no false success |
| R4 | Business API restart does not create a second controller | control revision/state before/after and row counts |
| R5 | Agent restart rotates runtime_generation | G0, G1, restart timestamps |
| R6 | Old lease, transport, Session binding, and old-generation resume fail immediately | negative result codes/closures with no effect |
| S1 | Logs contain no sensitive bodies, tokens, credentials, lease IDs, or secret path contents | zero-match sanitized scan summary |
| S2 | Pending command, inbox, outbox, owner-control, and journal backlogs return to baseline | before/after counts |
| B1 | Nginx rollback restores exact prior managed config and health | backup/digest, nginx -t, reload, canaries |
| B2 | Cloud release rollback succeeds | previous/current release IDs, unit and health evidence |
| B3 | Agent and Connector rollback succeeds | activation receipts, old release digests, UDS/WSS and prompt canary |
| E1 | Sanitized evidence package verifies | artifact ID, SHA-256, SHA256SUMS result |
| E2 | Operator and independent reviewer sign off | names, UTC, explicit PASS decision |

## 15. Draft exit procedure

Only after all acceptance rows pass:

1. Re-fetch PR #1 and verify its head still equals the accepted candidate.
2. Re-check the four workflow conclusions and artifact availability.
3. Add one sanitized Issue #2 comment containing:
   - successful Nginx and real-full-chain workflow URLs;
   - receipt and evidence artifact IDs and SHA-256 values;
   - candidate, Core, runtime bundle, and Cloud bundle SHA-256 values;
   - G0 and G1;
   - command IDs and terminal states without payloads;
   - component restart and rollback UTC timestamps;
   - backlog and log-scan pass summaries;
   - operator and reviewer sign-off.
4. Check every Issue #2 acceptance box.
5. Have the reviewer independently download and verify the evidence checksums.
6. Close Issue #2 only after the reviewer records PASS.
7. Mark PR #1 Ready for Review only after Issue #2 is closed and the PR head has not changed.
8. If the PR head changed at any point, return it to Draft and rerun the affected candidate build, deployment, and acceptance steps.

A green repository CI state or a green prompt-only full-chain workflow is not sufficient to move PR #1 out of Draft.
