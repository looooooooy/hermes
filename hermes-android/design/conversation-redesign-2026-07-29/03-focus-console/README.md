## Variant: Focus Console

### Design stance
When pending input exists, it replaces the composer instead of stacking above it; the bottom of the screen always has exactly one interaction system.

### Key choices
- Layout: compact header + current-execution strip + transcript + composer-replacement decision workspace
- Typography: strong current-state label, quiet historical process content
- Color: near-black neutral shell, cyan live state, semantic warning/recovery colors
- Interaction: Approval / Clarify / Recovery modes, radio-row selection, exact retry action

### Trade-offs
- Strong at: calm mobile composition, no dock/composer collision, clear current task
- Weak at: pending mode temporarily removes normal prompt/guide entry

### Best for
- 移动端快速观察当前执行，并处理单一权威 pending request
