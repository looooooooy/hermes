# Hermes Agent Output Parity Contract v1

Status: **Approved for implementation**

This document freezes the user-approved product contract for rebuilding the Hermes Mobile conversation experience. It is the acceptance source for Android model, projector, Compose, Markdown, control, Approval, Clarify, and physical-device work.

## 1. Objective

For the same authoritative Hermes turn, Hermes Mobile must present the same:

- content;
- order;
- section boundaries;
- lifecycle state;
- disclosure semantics;
- streaming progression;
- partial output on failure/interruption;
- replay/reconnect result.

This is **semantic and experiential parity**, not literal terminal-cell rendering. Compose must use native layout/lines, scrolling, touch targets, IME behavior, accessibility, and mobile-safe expansion while preserving Hermes Agent output semantics.

Authoritative native references:

```text
/Users/apple/.hermes/hermes-agent/ui-tui/src/components/messageLine.tsx
/Users/apple/.hermes/hermes-agent/ui-tui/src/components/thinking.tsx
/Users/apple/.hermes/hermes-agent/ui-tui/src/components/streamingAssistant.tsx
/Users/apple/.hermes/hermes-agent/ui-tui/src/components/streamingMarkdown.tsx
/Users/apple/.hermes/hermes-agent/ui-tui/src/components/markdown.tsx
```

## 2. Product Surface

Hermes Mobile conversation is an **Inspect + Operate Agent console**:

1. compact session/control header;
2. transcript as the single primary surface;
3. sticky composer/pending-input dock.

The transcript must not become a social-chat UI.

## 3. Canonical Turn Order

A turn is an ordered list of stable keyed **event segments**. The user prompt leads the turn; every subsequent segment is rendered in the order in which the authoritative Agent runtime produced it. Section kind must never be used as a sorting key.

For example, this event stream:

```text
Thinking → Assistant narration → Tool call/result → Thinking → final Assistant text
```

must remain in exactly that visual order. It must not be regrouped as all Thinking, then all Tools, then one combined Assistant response.

The ordered segment kinds are:

- Todo update;
- Thinking / visible reasoning;
- Tool call with its progressively updated output/result;
- Subagent / delegation / MoA reference;
- Activity;
- Assistant text;
- Diff;
- Error / interruption / token summary / pending input.

Tool start creates one stable node at its occurrence position. Tool progress and completion update that node in place; they never move it or create a duplicate completion card. Completion-owned standalone sections, such as a Todo update or inline Diff, are fixed at the completion event's occurrence position rather than being pulled back to Tool start. If Thinking or Assistant text resumes after a Tool result, it creates or updates a later segment rather than mutating text that visually precedes the result.

An explicit `Response` boundary is rendered only before the terminal assistant response when process segments precede it and no later process segment follows it. Assistant narration may legitimately occur before a Tool and must remain there without being mislabeled as the final response.

Standalone timeline items remain independently typed:

- event;
- system/slash message;
- diff;
- standalone tool result;
- approval request;
- clarify request.

The first authoritative occurrence fixes a section's visual position. Later deltas, lifecycle updates, reconnect replay, and completion update the same stable item without moving it.

## 4. Required Canonical Identity

Every rendered item or section must expose enough identity to preserve replay and UI state:

```text
stableKey
turnId (when part of a turn)
sectionKind
revision or content revision
status
streaming
```

Stable identity drives:

- realtime delta merge;
- REST baseline + realtime merge;
- reconnect replay deduplication;
- LazyColumn keys;
- section disclosure persistence;
- scroll anchors;
- exactly-once pending input state.

A historical tool-role message without `toolCallId` must remain visible as a stable standalone/fallback tool result; it must never be silently dropped.

## 5. Section Behavior

### User prompt

- leading Hermes prompt marker;
- no chat bubble or avatar;
- natural multiline wrapping;
- selectable/copyable;
- long paste may collapse without losing content.

### Todo

- first-class process section;
- stable item IDs;
- pending, in-progress, completed, cancelled states;
- updates replace the matching item instead of duplicating the list.

### Thinking

- independent from Activity and Tools;
- expanded by default;
- only server-authorized visible reasoning is rendered;
- disclosure choice persists across deltas and temporary disappearance.

### Tool calls

A tool node may contain:

- name and concise call label;
- context/arguments;
- live output increments;
- result/summary/diff;
- duration;
- running, complete, error, interrupted, unknown status;
- expand/collapse and copy affordances.

Output increments append to the same node. Failure retains arguments and partial output. No raw sensitive payload is exposed by default.

### Subagents / delegation / MoA

- represented as a nested process tree, not flattened tool text;
- stable parent/child identity;
- running/completed/failed state;
- elapsed time, progress/token summary when provided;
- visible MoA reference remains visible under native visibility rules.

### Activity

- independent section;
- hidden by default for ordinary activity;
- warning/error activity remains visible;
- full-detail mode may expose the complete trail.

### Response

If process sections precede an assistant response, render an explicit `Response` boundary. Assistant content uses Hermes response hierarchy, not a chat bubble.

### Event

Model switch, delegation completion, reconnect, and comparable events render as low-emphasis event lines rather than user/assistant messages.

### Error/interruption

- retain all partial Thinking, tools, and response content;
- distinguish failed from interrupted;
- append error state to the originating turn;
- do not replace the transcript with a generic error screen.

## 6. Disclosure Policy

Default native-equivalent policy:

```text
Thinking: expanded
Tools: expanded
Activity: hidden unless warning/error
Todo: expanded while active/incomplete; may collapse after completion
Subagents: summary visible, nested detail expandable
```

Overrides are keyed by stable `(turnId, sectionKind)` and must survive streaming updates, tool count changes, history prepend, and temporary section absence.

## 7. Visual Contract

### Composition

- flat transcript surface;
- no per-turn cards;
- no left/right chat bubbles;
- no avatars;
- no decorative gradients/glass;
- no rainbow status badges;
- cards/surfaces only for content needing containment: code, table, expanded tool payload, approval/clarify decision.

### Token posture

Semantic tokens, not hardcoded component colors:

```text
accent
border/rail
error
muted
prompt
statusBackground
statusForeground
text
 tool
warn
```

Dark: near-black/deep-navy shell, warm light text, restrained amber/copper process accents.

Light: warm off-white shell, dark warm text, restrained amber/copper process accents.

Red is reserved for real failure; orange for warning/interruption/uncertainty.

### Density baseline

```text
phone horizontal content inset: 16dp
process gutter: 22–26dp
turn gap: 20dp
section gap: 6–10dp
body: 16sp / 24sp
process/meta: 13–14sp / 19–20sp, monospace
code: ~13.5sp / 20sp, monospace
containment radius: 8–12dp
```

Terminal tree glyphs may appear as text content only when semantically required. Compose layout rails must be native lines/nodes, not Unicode indentation.

## 8. Markdown Parity

Required blocks and spans:

- ATX and Setext headings;
- bold, italic, strike, highlight;
- inline code;
- ordered, unordered, nested, and task lists;
- block quotes;
- tables with local horizontal scrolling;
- footnotes;
- links, autolinks, email links;
- fenced code with opener type/length matching;
- diff;
- inline/display math (`$…$`, `\\(…\\)`, `$$…$$`, `\\[…\\]`);
- `MEDIA:` and supported attachment presentation;
- ANSI sanitization;
- copy and safe link-open behavior.

### Streaming scanner invariants

- scanner state persists across append-only deltas;
- only complete newline-terminated lines advance scanner state;
- settled top-level blocks are append-only and parsed once;
- only the in-flight tail is reparsed;
- code fence opener character and length are tracked;
- mismatched/shorter closers do not close a fence;
- math delimiters inside code fences are inert;
- unmatched code/math opener remains in the unsettled tail;
- scanner resets if new text no longer extends the scanned prefix;
- repeated identical input is idempotent.

## 9. Scroll Contract

When at latest:

- incoming response/tool deltas remain visible;
- scrolling is stable and does not visibly jump.

When the user scrolls backward:

- follow mode stops immediately;
- incoming deltas never pull the viewport down;
- a compact `Back to latest` affordance shows accumulated updates.

History prepend:

- preserves visible item key and pixel offset;
- does not reset disclosure state;
- does not force latest-follow mode.

Returning to latest restores follow mode and clears accumulated-update indication.

## 10. Composer and Control

- sticky multiline composer with IME-safe layout;
- ViewModel owns draft and submission lifecycle;
- idle controller exposes Send;
- running controller exposes Stop as the active mutation;
- observer/conflict/lost/acquiring modes fail closed;
- unknown command delivery reconciles the original request ID and never blindly retries;
- Stop remains pending after RPC acceptance until authoritative realtime reports `running=false`;
- runtime rollover, transport loss, session exit, and authentication loss revoke mutation rights and clean up control lifecycle.

Control/observer sockets remain separate. Mobile must never call session resume/activate or replace the authoritative owner transport.

## 11. Approval and Clarify

Approval and Clarify are canonical pending-input sections, not generic dialogs or tool text.

Approval:

- stable request ID;
- server-redacted description/command only;
- only server-provided choices;
- exactly-once response;
- typed resolved/expired/conflict state;
- `allow_always` only when advertised and with confirmation.

Clarify:

- stable request ID;
- server choices plus Other when allowed;
- recoverable draft/selection;
- exactly-once answer;
- restored pending snapshot after reconnect.

Pending input may remain visible near the composer while retaining its transcript position.

## 12. Performance Contract

- stable LazyColumn item keys;
- immutable/persistent presentation models where practical;
- only the active/changed block recomposes per delta;
- settled Markdown blocks are not reparsed;
- large tool outputs use bounded preview plus explicit full view/copy;
- no whole-transcript re-projection caused by cursor animation;
- no duplicate layout items after replay or resync.

## 13. Security and Data Boundaries

Never render or log by default:

- access/refresh token;
- WS ticket;
- password;
- lease ID;
- credentials/Cookie/Authorization;
- complete sensitive approval payload;
- unredacted raw tool payload.

Raw diagnostic information is collapsed, recursively redacted, and omitted entirely when safe presentation is impossible.

## 14. Verification Matrix

Automated tests must cover at least:

- canonical section order and stable keys;
- independent disclosure persistence;
- assistant text delta append and completion merge;
- tool start/output delta/completion/error/interruption;
- tool fallback without `toolCallId`;
- Todo and subagent stable updates;
- Activity default/warn/error visibility;
- partial output retained on failure;
- replay/reconnect deduplication;
- REST/realtime merge;
- Markdown scanner fence/math/reset/idempotency;
- long response/tool output bounds;
- at-latest follow;
- backward-scroll pause;
- history prepend anchor;
- return-to-latest restoration;
- Send/Stop/unknown delivery/control-loss invariants;
- Approval/Clarify exactly-once and restored pending state;
- accessibility labels/focus/touch targets.

Final acceptance is vivo physical-device only and includes dark/light, Chinese/English, long Markdown/code/table, live tool output, manual backward scroll, IME, Send/Stop, Approval/Clarify, reconnect, and real Hermes session E2E.

## 15. Explicit Non-Goals

This rebuild does not authorize:

- a second Agent runtime on Android;
- a second session owner;
- server/runtime ownership redesign unrelated to output parity;
- exposing hidden private reasoning;
- hardcoding production credentials;
- replacing Hermes semantics with generic chat conventions;
- emulator evidence as final physical-device acceptance;
- commit, push, deploy, reset, or clean without explicit user authorization.
