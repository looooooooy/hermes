# Hermes Web visual QA

## Evidence

- Reference: `/Users/apple/Downloads/Hermes Web 手机能力对齐预览.png`
- Reference dimensions: 1672 × 941 pixels
- Desktop implementation: `docs/qa/desktop-conversation.png`, 1440 × 1000 viewport
- Mobile Conversation: `docs/qa/mobile-conversation.png`, 390 × 844 viewport
- Mobile Subagents: `docs/qa/mobile-subagents.png`, 390 × 844 viewport
- Mobile Long conversation: `docs/qa/mobile-long-conversation.png`, 390 × 844 viewport
- Production login: `docs/qa/production-login.png`, 390 × 844 viewport
- Production lease-only controller: `docs/qa/production-controller-lease-only.png`, 390 × 844 viewport
- Verified state: development preview fixture, connected controller, session `mobile-56f3`

## Full-view comparison

The final comparison placed the reference and all four implementation screenshots in the same visual review. The implementation preserves the source's dark terminal surface, cyan/green/amber/red state language, restrained borders, monospace operational content, compact cards, bottom composers, and dense long-event stream. Desktop expands the same content into a two-column working surface; it does not introduce a left-side timeline or navigation rail.

## Focused-region checks

- Conversation: execution order remains prompt → Thinking/Todo/tool → Hermes stream → queue → approval. Thinking, Todo, and tool calls are flat conversation-flow rows; there is no left rail, node chain, or timeline pseudo-element.
- Subagents: coordinator remains visually primary; children are indented with parent connectors; the two peer tabs split the 362-pixel inner rail evenly at 181 pixels each; all five cards and the action row remain usable at 390 × 844.
- Long conversation: the right edge uses 25 compact line markers, not a conventional scrollbar or left timeline. The header reports the fixture's authoritative `1,248 lines` count.
- Approval: cyan Approve and red Deny retain equal visual weight and remain accessible in Conversation and Long conversation. In the 390 × 844 Conversation capture, the approval bottom is 685.66 px and the composer begins at 695.66 px, so both actions and the full composer are visible without overlap in the first viewport.
- Responsive behavior: measured document width equals viewport width at both 390 and 1440 pixels; no horizontal overflow was found by the executable Playwright suite.
- Production boundary: the optimized build opens on a real login form. The browser smoke verifies login, cookie-auth ticket requests, observer subscription, and lease acquisition with no development fixture or browser token. When only lease methods are advertised, the screen identifies the limitation and disables conversation mutations.

The long-navigation region received the focused interaction check because it is the distinctive requested behavior. Activating the `Approval required` marker with Enter moved keyboard focus to `long-event-approval`.

## Fidelity ledger

1. Matched the near-black shell, thin blue-gray outlines, and low-elevation surfaces instead of introducing dashboard cards or gradients.
2. Matched the reference's compact mono typography and timestamp hierarchy while retaining a clearer sans-serif product name.
3. Matched green active dots, amber queued state, gray waiting state, cyan input state, and red failure/deny state.
4. Matched the reference conversation density and disclosure behavior for Todo and tool output without the rejected left-side process rail.
5. Matched the coordinator/child hierarchy, dashed queued card, and Guide/Stop/Send action grouping.
6. Matched the long-conversation event density and right-side short-line navigation, including colored event anchors.
7. Preserved a bottom composer on Conversation and Subagents and an inline approval surface in the long view.

## Intentional differences

- The browser implementation does not draw a phone bezel, mobile status bar, or home indicator; screenshots represent the actual Web viewport rather than a device mockup.
- The desktop layout uses available width for a two-column conversation/approval arrangement while preserving the same content order.
- View switching is available from the top-left menu. Conversation and Long conversation have no global tab bar; Subagents alone shows the reference's `Conversation` and `Subagents` peer tabs.

## Copy comparison

Operational labels and fixture content follow the approved reference: `Hermes Web`, `Controller`, `Thinking`, `Todo`, `Tool calls`, `Subagents`, `Long conversation`, `Input required · Approval`, `Approve`, `Deny`, `Guide`, `Stop`, and `Send`.

## Comparison history

1. Initial mobile capture exposed excessive Subagents card height and a stale long-event line count.
2. Density was tightened so all hierarchy cards and actions fit; line count was moved into explicit fixture state and rendered as `1,248 lines`.
3. The rejected global tab row and Conversation process timeline were removed.
4. Final Playwright Chromium captures confirmed the three states, 390/1440 horizontal-fit metrics, complete first-screen approval/composer geometry, and keyboard navigation target.
5. Production Chromium smoke captured the login screen and the authenticated lease-only controller state from the optimized bundle.
6. The two-tab rail was corrected from a stale three-column grid, and screenshot capture now waits for the post-navigation compositor frame so shared header/session chrome is present in every retained mobile artifact.

## Final result

passed
