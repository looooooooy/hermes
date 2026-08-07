# Hermes Mobile

Cross-platform remote access for a single authoritative Hermes Agent runtime.
H5/PWA is the active first-closure client and connects only through Hermes Cloud;
Android work is paused for this milestone. Both clients reuse the independent
Connector and Agent Local Gateway boundaries rather than creating another Agent.

## Product invariants

- The PC or server is the only Agent execution authority.
- Android and Desktop may observe the same session concurrently.
- A session has at most one controller.
- SessionDB is authoritative; Cloud and client storage are bounded read caches.
- Unknown message delivery is reconciled by client turn ID before retry.
- Model/provider credentials never leave the Hermes execution host.
- Hermes Connector is an independent service and does not import Agent internals.
- The public service never exposes the local Hermes Dashboard directly.

## Architecture

The active cross-component execution plan and evidence ledger is
[`docs/plans/feature-commercial-connector-cloud-1.md`](docs/plans/feature-commercial-connector-cloud-1.md).
The separately authorized Hermes Core work is mapped in
[`docs/plans/hermes-core-host-spi-v1-patch-map.md`](docs/plans/hermes-core-host-spi-v1-patch-map.md).

The approved commercial architecture is documented in
[`docs/2026-07-28-hermes-connector-commercial-architecture-design.md`](docs/2026-07-28-hermes-connector-commercial-architecture-design.md).

The customer-installation, managed-runtime, release, update, rollback, recovery,
and uninstall productization baseline is documented in
[`docs/2026-08-07-hermes-managed-runtime-customer-installation-design.md`](docs/2026-08-07-hermes-managed-runtime-customer-installation-design.md).

Current status: the Cloud revision 11 candidate is complete only in the local
tree and has not been deployed. The public test server still runs the older
SQLite release with deterministic `android-agent` / `android-bootstrap` seed
data; those names do not represent an Android Agent or a real Hermes session.
Hermes 0.19 still lacks the public Host SPI for live Observer/Control, so the
Plugin and Connector fail closed for the real local Agent path.

The local H5 backend slice now uses secure cookie login/logout, canonical
`/api/v1/agents` and `/api/v1/agents/{agent_id}/sessions` directory routes, and
server-side invalidation of pre-logout access and WebSocket-ticket authority.
The executable integration gate runs the production browser clients through the
real Vite proxy into a real Cloud ASGI process backed by temporary SQLite ORM
state. Its `integration-agent` and `Integration session` rows are deterministic
fixtures, not proof of a connected local Hermes runtime. Remote deployment and
the Connector/Plugin/Hermes portion of the full chain remain pending.

The future enterprise AI workbench expansion is documented in
[`docs/2026-07-28-enterprise-ai-workbench-expansion-design.md`](docs/2026-07-28-enterprise-ai-workbench-expansion-design.md).

The future agent-native enterprise operating model is documented in
[`docs/2026-07-28-hermes-agent-native-enterprise-operating-model-design.md`](docs/2026-07-28-hermes-agent-native-enterprise-operating-model-design.md).

The canonical design for work-information governance, file and system data,
agent data exchange, AI data processing, visualization, integration, and
low-maintenance platform evolution is documented in
[`docs/2026-07-29-hermes-agent-native-data-governance-and-ai-data-platform-design.md`](docs/2026-07-29-hermes-agent-native-data-governance-and-ai-data-platform-design.md).

The owner-level operating philosophy, dual ILPO, growth, and organizational AI
strategy is documented in
[`docs/2026-07-29-hermes-ai-native-enterprise-owner-operating-philosophy.md`](docs/2026-07-29-hermes-ai-native-enterprise-owner-operating-philosophy.md).

The decision-ready investment case, evidence review, value realization path,
ROI/TCO model, stage gates, and low-maintenance operating model are documented in
[`docs/2026-07-29-hermes-ai-native-enterprise-investment-business-case.md`](docs/2026-07-29-hermes-ai-native-enterprise-investment-business-case.md).

The H5/PWA client is isolated under [`hermes-web/`](hermes-web/README.md) and is
the current client-closure target. The paused native Android client remains
isolated under [`hermes-android/`](hermes-android/README.md); its source, Gradle
project, scripts, tests, and retained HTML design references all stay there.

## Repository boundaries

- `hermes-agent-plugin/`: the Agent-hosted plugin; its only Python package is
  `hermes_agent_plugin`, with OS adapters separated under
  `adapters/platform/{macos,linux,windows}/`.
- `hermes-connector/`: the independent local service, with OS adapters separated
  under `adapters/platform/{macos,linux,windows}/`.
- `hermes-cloud/`: the public and internal Cloud services; SQLite and PostgreSQL
  infrastructure remain separate platform adapters.
- `hermes-web/`: the H5/PWA client; production code talks only to the Cloud
  public REST and realtime boundaries.
- `hermes-android/`: the complete Android project, including retained HTML design
  sources. No Android Gradle project or application source belongs outside it.
- `contracts/`: the cross-component protocol authority; generated consumer copies
  are synchronized, never edited as competing sources.

## Build

```bash
cd hermes-web
npm run test:app
npm run typecheck
npm run lint
```

The repository-level cross-module E2E suite runs from the repository root with
the Cloud virtualenv interpreter (the system `python3` lacks the dependencies):

```bash
hermes-cloud/.venv/bin/python -m pytest tests -q
```

Observer pipeline cases that register the production plugin against the public
Host SPI are opt-in live tests: they skip unless `HERMES_E2E_LIVE_HOST=1` is set
and `hermes_cli.extension_host_v1` is importable (a Hermes Core tree carrying
the gateway-extension Host SPI v1 on `PYTHONPATH`). Without them the real Host
path fails closed by design, so the cases report `skipped` instead of failing.
