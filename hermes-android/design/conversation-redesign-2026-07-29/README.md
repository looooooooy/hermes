# Hermes Mobile Conversation Redesign · Round 1

Status: design exploration only — **not approved for implementation**.

## Objective
Replace the current box-heavy conversation screen with a production-grade **Inspect + Operate Agent console**. All directions preserve canonical event ordering, stable process semantics, safe Approval/Clarify/Recovery identity, and mobile-native interaction.

## Head-to-head

| Dimension | Quiet Ledger | Inline Operations | Focus Console |
|---|---|---|---|
| Primary stance | Content-first ledger | Chronological audit timeline | Current-task focus |
| Pending placement | Single elevated dock | Inline event + sticky locator | Replaces composer |
| Transcript visibility | Medium | Highest | Medium-high |
| Decision immediacy | Highest | Medium | High |
| Visual calm | High | Medium-high | Highest |
| Long-session audit | High | Highest | Medium |
| Mobile one-handed use | High | Medium | Highest |

## Recommendation
**Focus Console** is the strongest mobile composition because it removes the current triple-stack failure (transcript + pending dock + disabled composer). Use **Quiet Ledger's process rail and tool treatment** inside it. Inline Operations is best retained as an audit/history mode rather than the default live-control surface.

## Non-negotiables for the selected direction
- No social chat bubbles or avatars.
- No equal-weight card collage.
- No decorative gradients, glass, or rainbow badges.
- Pending input is a state of the primary interaction surface, not an extra dashboard widget.
- Response, process, tool, Todo, Subagent, error and interruption remain independently readable.
- TalkBack, 48dp targets, reduced motion, IME and physical vivo acceptance remain required after approval.

## Open locally

```bash
open design/conversation-redesign-2026-07-29/01-quiet-ledger/index.html
open design/conversation-redesign-2026-07-29/02-inline-operations/index.html
open design/conversation-redesign-2026-07-29/03-focus-console/index.html
```
