# Hermes Android

Native Android client for observing and operating one authoritative Hermes
Agent session through Hermes Cloud.

## Modules

- `app/`: Compose UI, authentication, session projection, observer/control
  coordination, pending Approval/Clarify interaction, and Android tests.
- `core/protocol/`: REST and WebSocket protocol types, strict Cloud realtime
  validation, and vendored mobile-control contract fixtures.
- `scripts/`: physical-device guard and vivo transcript-performance collection.
- `design/`: approved Android interaction source material retained beside the
  implementation.

The root `../contracts/` directory remains the cross-component source of truth.
Android's vendored control contract is synchronized into
`core/protocol/src/test/resources/contracts/`.

Realtime sockets accept only the exact role-specific `gateway.ready` shape.
Control mutations are enabled per connection from
`control_available_methods`; an omitted method remains read-only and no RPC is
sent. Reconnects discard the previous capability set before applying the new
handshake.

## Production UI and design sources

The production path is `MainActivity` → `SessionBrowserScreen`; the HTML files
are reference material, not separate demo pages.

- `design/conversation-redesign-2026-07-29/04-hermes-agent-composer/` defines
  the single multiline composer, Guide/Queue/Stop behavior, canonical Todo and
  Subagent hierarchy, and long-running work locator.
- `design/conversation-redesign-2026-07-29/05-authoritative-active-work/`
  tightens the authoritative active-work and interruption states.
- `design/approval-clarify-state-board/` defines Approval, Clarify, frozen
  response identity, and same-request recovery.

These structures are implemented by the existing transcript, current-execution,
long-running work, composer, and pending-input components. They must be evolved
in place; do not create a parallel showcase screen.

## Build and test

Run from this directory:

```bash
./gradlew :core:protocol:test :app:testDebugUnitTest
./gradlew :app:compileDebugAndroidTestKotlin :app:lintDebug
./gradlew :app:assembleDebug :app:assembleDebugAndroidTest
```

With a connected test device or emulator:

```bash
./gradlew :app:connectedDebugAndroidTest
```

Debug APK:

```text
app/build/outputs/apk/debug/app-debug.apk
```

Physical vivo validation:

```bash
scripts/vivo-transcript-performance.sh <physical-serial>
```
