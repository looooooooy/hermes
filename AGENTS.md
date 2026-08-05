# Repository Instructions

1. Preserve the approved Hermes Mobile architecture: one authoritative Agent runtime, multi-client observation, one controller.
2. Use strict test-driven development for behavior changes: failing test, minimal implementation, green suite.
3. Keep Android local storage a projection; server sessions remain authoritative.
4. Do not log tokens, secrets, full approval payloads, or tool output by default.
5. Do not commit, push, deploy, or modify Hermes user configuration without explicit user instruction.
6. Build and test every completed vertical slice.
