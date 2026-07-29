# Hermes Mobile

Cross-platform remote access for a single authoritative Hermes Agent runtime.
H5/PWA is the primary commercial client. The existing Android application remains
a native reference client and protocol compatibility surface.

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

The approved commercial architecture is documented in
[`docs/2026-07-28-hermes-connector-commercial-architecture-design.md`](docs/2026-07-28-hermes-connector-commercial-architecture-design.md).

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

The existing Android executable slice discovers a configured Hermes endpoint and
remains useful for protocol parity tests. New commercial delivery follows the
H5/PWA, Remote Server, independent Connector, and Agent Local Gateway boundaries.

## Build

```bash
./gradlew test lint assembleDebug
```
