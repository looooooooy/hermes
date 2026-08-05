# Hermes Web

Hermes Web is the browser client for the existing Hermes Mobile control plane. It preserves the repository invariant of one authoritative Agent runtime, many observers, and at most one controller. The Web client talks only to the Cloud public boundary; it does not connect directly to Plugin or Connector.

## Current vertical slice

- Production observer-v2 projection for authoritative user/assistant snapshots, Thinking/reasoning, Todo lifecycle, Subagent orchestration, stable tool and terminal lifecycle/output, streamed Hermes output, status, approval/clarification, and composer controls. Historic and live tool output remains attached to a stable disclosure node and is collapsed by default.
- Subagent `parentId` renders as a bounded 128-node, 8-level nested tree with Active/Queued/Waiting/Stopped states and real Guide, Stop, and Send command controls.
- Long-conversation view with compact event rows, observer-v2 Subagent lifecycle and authoritative pending-input audit rows, a keyboard-operable right-side short-line navigator, and approval handling.
- Responsive phone and desktop layouts based on the approved mobile-parity reference, including a bounded, ellipsized authoritative controller label that cannot displace the menu or product title at 390px.
- Strict `hermes.tui.v2` observer and `hermes.tui.v1` control adapters: version-bound short-lived single-use ticket minting, exact server-selected subprotocol and ready/subscribe handshake, atomic projection snapshot plus fully decoded replay, sequence/revision/generation fail-closed resync, dynamic control capabilities, and correlated RPC result/error handling.
- Recursive display-safe gates scan v2 event payloads, snapshot messages, inflight text, lifecycle collections, replay events, extensions, authoritative controller labels, and control pending-input display fields before projection or rendering. They reject private extension fields, credential-like values, control characters, ill-formed Unicode, and generated depth/field/array limit violations; bounded namespaced metadata, benign display text, and nonnegative aggregate token counts remain available.
- Production password login and server-confirmed logout using secure same-origin cookies, followed by the authoritative Agent directory and Agent/profile-scoped real-session directory. The browser never reads an HttpOnly cookie and does not store an access token in local or session storage; a failed logout leaves the current runtime and authenticated UI intact.
- Controller lease lifecycle covering exact acquire, status reconciliation, renewal, visibility/unmount release, and fail-closed loss handling.
- Runtime changes revoke the old lease before any new action can run. Best-effort release keeps the old runtime binding, and only the latest runtime epoch may install a replacement lease.
- An acquired lease becomes actionable only when the authoritative status matches its mobile controller kind, label, revision, and expiry. Desktop, local, or no-controller status keeps Web non-controller and exposes the authoritative label.
- Injectable command port for prompt, steer, interrupt, and approval operations. UI success is rendered only after an authoritative RPC result; Stop waits for the server `running=false` event.

## Directory boundaries

```text
src/
  app/                 application state, reducer, and shell
  dev/                 development-only preview fixtures and fixture adapter
  features/            conversation, subagents, and long conversation
  platform/web/auth/   browser password-auth boundary
  platform/web/catalog/ strict Agent and real-session directory boundary
  platform/web/realtime/ browser realtime, ticket, lease, and command adapters
  production/          production login/runtime composition
  shared/contracts/    Cloud public-contract decoders
  shared/ui/           shared interaction components
```

`src/dev/` is dynamically loaded only while `npm run dev` is running. That command is an explicit fixture preview for UI development; it is not a Cloud integration run. A production build always starts at the password login screen and never presents fixture data as a real Cloud connection. Login posts the frozen Basic-provider request to `/auth/password-login` with `credentials: include`, reads `/api/v1/agents`, and then reads `/api/v1/agents/{agent_id}/sessions?min_messages=0&archived=exclude&order=recent&limit=…&offset=…`. The user selects a returned Agent and session; the returned session profile is passed unchanged into the runtime. Agent A and Agent B are directory rows handled by the same code path. Stale Agent/session/load-more reads are aborted on switch, logout, and unmount without being presented as network failures. The optional `?session=<session-key>` query is honored only after an exact match across the selected Agent's authoritative directory, with a hard maximum of 20 page requests and 400 advertised records; it never creates a runtime directly. Cross-page identity is the authoritative `(agent_id, profile, session_key)` tuple, so a repeated lineage under another row ID fails closed. Web then asks `/api/auth/ws-ticket` for observer and control tickets using the secure browser session. Logout posts an empty body to `/auth/logout` with the same HttpOnly cookies and clears the local runtime only after the server returns the exact `{ "ok": true }` response. The observer socket requests and verifies `hermes.tui.v2`; the separate control socket requests and verifies `hermes.tui.v1`. Credentials, cookies, tickets, lease IDs, approval payloads, and tool output are not logged.

The complete `(agent_id, profile, session_key)` tuple is passed through runtime
construction and included in both observer and control ticket requests.
Selecting the same profile/session key under another Agent therefore selects a
different binding without a different code path. Login, logout, ticket minting,
and stale runtime creation are abortable; unmount, replacement login,
disconnect, session switch, and stop invalidate the prior lifecycle epoch so a
late response cannot install state or reconnect.

Password, catalog, and ticket JSON responses share one bounded Web Streams
reader. It checks `Content-Length` before reading, counts bytes incrementally
for chunked bodies, cancels immediately on overflow or abort, and retains each
client's exact response decoder after the bounded JSON parse.

The only persistent browser identity is `hermes.web.client_instance_id.v1`: a non-sensitive canonical UUID stored in `localStorage` so refresh/crash recovery can use Cloud's bounded same-client grace. Tokens, leases, session payloads, approvals, and tool output are never stored there. Missing or malformed IDs are replaced with a newly generated canonical UUID without logging the value.

## Local workflow

```bash
npm install
npm run dev          # fixture preview only
npm run dev:cloud    # production H5 through the Cloud proxy
npm test
npm run typecheck
npm run lint
npm run build
npm run test:sites
npm run test:browser
npm run test:browser:production
npm run test:cloud-preview
```

`npm test` is the standard local test gate. Separate named scripts run the application suite, the offline IANA parser/comparator suite, the production-bundle budget policy suite, and the fifteen Cloud-preview process runtime tests exactly once, so registry normalization, bundle-budget enforcement, and process exit-race checks cannot be hidden by the application test-file glob.

`npm run dev:cloud` first runs the production build and its fixture-sentinel gate, then serves that production UI with Vite preview on `localhost`. The preview proxies browser-same-origin `/auth/**`, `/api/**`, and `/api/ws` requests to `https://api.seaotter.wiki/hermes/` by default; bounded `/live` and `/ready` routes are available for no-secret Cloud health checks. To use another HTTPS Cloud hostname or global IP literal, override the base URL without changing browser code:

```bash
HERMES_WEB_CLOUD_URL=https://cloud.example/hermes/ npm run dev:cloud
```

The proxy accepts an HTTPS Cloud hostname or a global IP literal; domain-name resolution follows the machine's standard network path, including a local TUN or proxy DNS, while TLS hostname verification remains enabled. It rejects port `9119`, loopback hostnames, and non-global IP literals. The proxy changes the upstream Host and Origin, enables WebSocket forwarding, removes the Cloud cookie Domain, and rewrites its Path to `/`. Keep the browser URL on the printed `localhost` address so Secure cookies use the browser's localhost secure-context handling. The Web application still uses only same-origin URLs and never receives an address for a local Agent.

Global IP-literal classification uses independently versioned vendored IANA IPv4 and IPv6 Special-Purpose Address Registry snapshots, both currently dated `2025-10-09`, so regular tests remain offline. Run `npm run check:iana-registry` explicitly when maintaining them; it uses the network to normalize and compare every official `{family, cidr, globallyReachable}` tuple—including `false`, `null`, and multi-address rows—plus each registry's own version and source record count.

The required runtime chain is `H5 -> Cloud -> Connector -> Plugin -> local Hermes`. Reaching the Cloud login page or successfully authenticating proves only the H5-to-Cloud portion; it does not prove that Connector, Plugin, or the local Hermes runtime is connected.

`npm run build` includes a production-bundle gate that rejects development fixture sentinels and browser-token storage markers. It also enforces aggregate production limits of 8 assets, 450,000 raw/130,000 gzip JavaScript bytes, and 40,000 raw/12,000 gzip CSS bytes.

The development browser suite runs executable 390 × 844 and 1440-pixel DOM/overflow checks. It verifies the first phone viewport contains the complete approval controls and composer, Conversation has no timeline rail, only Subagents owns the two peer tabs, and Long conversation retains its right-side short-line navigator. The separate production-mode smoke builds the optimized bundle and verifies password login → cookie-only Agent/session directory → authoritative session selection → cookie-auth ticket requests → observer subscription → control lease without any fixture or browser-stored token. Its strict HTTP and WebSocket responders are a deterministic browser boundary test, not evidence that the deployed Cloud/Connector/Plugin/Hermes chain is live.

## Current integration boundary

The Web side of Cloud login, Agent/session selection, observation, controller leasing, and dynamic action dispatch is implemented. Every action fails closed unless the server advertises that exact method and the application holds a current runtime/session/lease binding. Refreshing the directory, switching sessions, or signing out unmounts the old runtime so its tickets, sockets, and controller lease are cleaned up. A Cloud deployment that advertises only lease-management methods is shown honestly as lease-only: Queue, Guide, Send, Stop, and Approval stay unavailable with a visible reason. This module does not claim an action closed loop until the deployed Cloud/Connector path advertises and executes those action methods. Offline/PWA caching remains deferred.

`realCloudAuth.integration.test.ts` executes the production password-auth and session-catalog clients through the real Vite Cloud proxy against a real Cloud ASGI process backed by a temporary SQLite ORM database. It covers login, canonical Agent and Agent-scoped session reads, ticket minting, logout, access-cookie replay rejection, and rejection of a ticket minted before logout. The seeded `integration-agent` and `Integration session` rows are deterministic integration fixtures. This gate proves the H5-to-Cloud backend slice only; it is not evidence that Connector, Plugin, or a local Hermes runtime is connected.

The Cloud contract exposes Cookie authentication only on `GET /api/v1/agents` and `GET /api/v1/agents/{agent_id}/sessions`, while retaining Bearer compatibility on both directory reads. Cookie-only reads require effective HTTPS plus either an exact same-origin `Origin` or the complete same-origin Fetch Metadata triple; simultaneous Bearer and Cookie credentials must resolve to the same principal. Session detail and message routes remain Bearer-only. Hermes Web intentionally cannot read the HttpOnly access cookie and will not read or store an access token. `POST /auth/logout` requires an empty, exact same-origin HTTPS request, returns exactly `{ "ok": true }`, and expires the access, refresh, and provider cookies only after durable server-side revocation; absent cookies and already-revoked sessions are idempotent success, while malformed/conflicting cookies or revocation failure preserve the active browser state for a safe retry. Contract publication is not deployment evidence, so production H5 continues to fail closed if the deployed Cloud does not match this boundary.

Production explicitly requests observer contract 2 and rejects a v1 ready frame instead of silently downgrading. The v2 decoder is driven by synchronized generated contracts, including the sole `session-event-v2` schema dependency used for live and every replay item. Observer v1 remains an explicit compatibility mode for the existing transcript and displays that Todo, Subagent, tool, and terminal lifecycle parity is unavailable; it never synthesizes those projections.

`CloudV2ProtocolIntegration.test.ts` composes the production HTTP ticket provider and realtime adapter against a strict in-process Cloud protocol harness. It verifies exact v2 ticket binding and single-use presentation, selected subprotocol, ready/subscribe, snapshot/replay/live lifecycle, gap and runtime-rollover resync, downgrade rejection, and the independent approval/control-v1 lane. This is a deterministic Web boundary gate; the deployed Cloud-process canary remains a cross-stack rollout condition.

## Real full-chain acceptance gate

`scripts/real-full-chain-gate.mjs` is the release-side CLI for an explicitly selected, non-fixture Hermes session. It does not use development data and does not choose an endpoint, account token, session, or prompt implicitly. A token or prompt file must be an owner-private regular file with no group or other permissions.

```bash
node scripts/real-full-chain-gate.mjs \
  --cloud-url https://cloud.example/hermes/ \
  --access-token-file /private/path/account-token \
  --agent-id 11111111-1111-4111-8111-111111111111 \
  --session-id 22222222-2222-4222-8222-222222222222 \
  --prompt-file /private/path/acceptance-prompt
```

The CLI fails closed unless Cloud is ready, Bearer authentication succeeds, the explicit target is a live authoritative `host_catalog` session, observer v2 and control v1 negotiate exactly, a controller lease is reconciled, `prompt.submit` is confirmed, ordered assistant streaming reaches `message.complete`, and a fresh-ticket observer reconnect preserves session and sequence continuity. It explicitly rejects catalog identities containing `test`, `demo`, or `fixture`, including `Hermes Cloud Test Session`. Output is a non-secret JSON receipt; configuration failures exit `2`, verification failures exit `3`, and success exits `0`.

Todo, Tool, Pending Input, and Approval are reported as `confirmed` only when authoritative evidence appears in the same scene. Otherwise the receipt says `independent_gate_required`. Run an explicit scene as an independent gate with `--require-evidence todo,tool,pending_input,approval` or any required subset. The Approval gate observes pending authority only; it never submits an approval response.
