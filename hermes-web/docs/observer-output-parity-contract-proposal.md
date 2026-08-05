# Observer Output Parity Contract Proposal

Status: **Approved contract; Web consumer implemented, deployment pending**

Hermes Web preserves the eight-event v1 decoder as an explicit compatibility path and now requests observer contract 2 in production. The v2 path consumes the synchronized `cloud-realtime-v2`, `observer-output-parity-v2`, and sole `session-event-v2` schema dependency; development preview fixtures remain isolated under `src/dev/`.

The former expected-failure capability gates in `src/shared/contracts/observerOutputParityContract.test.ts` are ordinary passing v2 contract tests. Web does not add private lifecycle event names to the v1 decoder.

## Minimum Todo projection

The shared observer contract needs one versioned Todo lifecycle event with:

- stable turn ID;
- stable Todo section ID;
- monotonic content revision;
- ordered items containing stable item ID, formal label, and `pending | in_progress | completed | cancelled` status;
- section lifecycle status;
- outer authoritative `event_sequence` and normal replay rules.

The observer snapshot must carry the latest Todo sections using the same identities and revisions. Reconnect cannot reconstruct a correct Todo view from deltas alone.

## Minimum Subagent projection

The shared observer contract needs one versioned Subagent lifecycle event with:

- stable Subagent ID;
- nullable parent Subagent ID;
- monotonic content revision;
- display name, assigned goal, and server-provided summary;
- `queued | waiting | active | complete | stopped` lifecycle status;
- outer authoritative `event_sequence` and normal replay rules.

The observer snapshot must carry the latest Subagent nodes using the same identities and revisions. The contract must define removal or terminal retention and reject duplicate IDs, missing parents, and parent cycles.

## Approval conditions

Cross-stack rollout requires:

1. Plugin, Connector, and Cloud advertise and execute the approved capability and observer-v2 path.
2. Deployment verifies version-bound ticket, ready, subscribe, snapshot, replay, and live behavior without downgrade.
3. Android and Web synchronized generated resources remain equal to the repository authority.
4. Cross-stack gates retain stable update, reconnect replay, ordering, deletion, and malformed hierarchy fail-closed coverage.

Production accepts lifecycle events only through the generated v2 schema. V1 never accepts them.

## Web security and protocol gates

Web applies a recursive display-safe check after generated schema validation and before any v2 snapshot or event reaches projection or rendering. Event payload text and snapshot messages, inflight text, lifecycle collections, and replay events reject Basic/Bearer credentials, assignments, JWTs, private keys, and provider-token prefixes. Within `extensions`, sensitive compound keys such as credential, token, tool-argument, raw-output, private-reasoning, or approval-payload fields also fail closed. Generated nesting, object-field, array-item, string, and frame bounds remain authoritative; safe namespaced display metadata, benign text, and nonnegative aggregate `token_counts` pass.

The same semantic credential-value gate runs on the control-v1 pending approval and clarification object before controller state is published. A rejected value cannot reach the application reducer, DOM, long-conversation audit row, or close/error text.

The production Web ticket provider and realtime adapter are composed in a strict protocol harness covering exact `observer_contract=2` ticket binding, single-use ticket presentation, server-selected `hermes.tui.v2`, `gateway.ready`, exact subscribe, snapshot/replay/live lifecycle, runtime rollover, sequence gaps, and downgrade rejection. The independent approval/controller lane stays on `hermes.tui.v1`. This harness does not replace the required deployed Cloud-process canary in the approval conditions above.
