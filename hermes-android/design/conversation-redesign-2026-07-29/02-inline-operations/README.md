## Variant: Inline Operations

### Design stance
Pending input stays at its authoritative occurrence in the timeline; the sticky bottom element is only a lightweight locator.

### Key choices
- Layout: timestamped operational timeline + inline pending event + compact pending anchor
- Typography: dense event metadata and readable body hierarchy
- Color: quiet neutral shell, restrained cyan, amber only for pending
- Interaction: state switching, inline choice selection, jump-to-pending anchor

### Trade-offs
- Strong at: chronological truth, maximum transcript visibility, minimal overlay
- Weak at: action can sit lower in a long event stream; requires the sticky locator

### Best for
- 审计、回放和长生命周期 Agent session
