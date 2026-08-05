## Extension: Hermes Agent Composer

Status: HTML interaction design only — not yet approved for Android implementation.

### Design stance

Continue the selected **Focus Console + Quiet Ledger** direction while making the bottom interaction surface match Hermes Agent: multiline input and runtime actions share one authoritative composer container.

### Required content represented

- Dialogue remains the primary surface; no social chat bubbles or avatars.
- Todo is an ordered, in-place lifecycle section with completed, active and queued items.
- Subagents are process children with goal and status, never independent chat participants.
- Parent Hermes retains the only response boundary and controller authority.
- Running input makes **Guide**, **Queue**, **Stop**, voice and send behavior explicit inside one composer.

### Interaction states

- **Running**: Todo and Subagents visible; Queue is the default action.
- **Todo focus**: Todo expanded and Subagents collapsed without reordering the transcript.
- **Subagent focus**: Subagents expanded and Todo collapsed.
- **Complete**: running controls disappear and the composer returns to ordinary Send behavior.
- Section headers collapse independently; Guide/Queue, Voice, Stop and Send are interactive.

### Trade-offs

- Strong at: preserving one conversation hierarchy, showing orchestration without a dashboard, and keeping the input model close to Hermes Agent.
- Weak at: a 390px screen can show only short Subagent summaries before requiring scroll; deep details still need disclosure.

### Open locally

```bash
open design/conversation-redesign-2026-07-29/04-hermes-agent-composer/index.html
```
