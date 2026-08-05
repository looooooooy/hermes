## Hermes Mobile · Approval / Clarify state board

### Design stance
A single coherent native transcript system shown across three high-risk interaction states, rather than three unrelated visual themes.

### States
- Approval: explicit second confirmation for `allow_always`.
- Clarify: exact predefined choice or one Other answer form.
- Recovery: frozen response payload and same-request retry after authoritative restoration.

### Key choices
- Layout: process-oriented transcript with a sticky input dock above the composer.
- Typography: native system text plus monospace for command/tool identity.
- Color: neutral dark surface; cyan for active/control, amber for uncertainty, red only for denial/failure.
- Interaction: Confirm, choice selection, Other input, and same-request retry have visible state transitions.

### Trade-offs
- Strong at: lifecycle clarity, dense technical output, safe mutation boundaries.
- Weak at: the sticky dock reduces visible transcript height on smaller devices.

### Best for
Hermes sessions where tool/process output remains visible while the authoritative runtime requests a controller-bound response.
