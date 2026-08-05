## Variant: Quiet Ledger

### Design stance
Transcript 是唯一主表面；只有 code、tool payload 与 pending decision 获得容器。

### Key choices
- Layout: compact header + native process rail + single elevated decision dock
- Typography: system body + monospace process/meta identity
- Color: near-black neutral shell, cyan control, green completion, amber confirmation
- Interaction: Approval / Clarify / Recovery state switching, choice selection, confirmation feedback

### Trade-offs
- Strong at: hierarchy, context retention, semantics, realistic dense sessions
- Weak at: expanded pending dock still reduces transcript height

### Best for
- 高频查看长运行轨迹，同时偶尔执行高风险决策
