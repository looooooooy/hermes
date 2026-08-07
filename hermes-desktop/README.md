# Hermes Desktop

Hermes Desktop is the cross-platform customer shell for Hermes Managed Runtime.
It is intentionally **not** the Agent execution authority.

## Product boundary

```text
Hermes Desktop (Tauri 2 + Svelte)
        ↓ local manager IPC
hermes-runtime-manager (Rust)
        ↓ lifecycle authority
Managed Runtime
├── Private CPython
├── Hermes Core
├── Agent Plugin
└── Connector
```

Closing the desktop window must not stop the local Agent or Connector. The native
Runtime Manager owns install/start/stop/update/rollback/recovery. The UI only
requests operations and renders evidence.

## Design direction

The first UI slice is a Runtime Cockpit rather than a generic admin dashboard.
The opening screen answers three questions immediately:

1. Is the local Agent actually ready to execute?
2. Is the Cloud transport actually connected?
3. Is the active Managed Runtime verified and safe?

The visual system uses a restrained ink-teal desktop palette, low chrome, thin
semantic borders, high information hierarchy, and status color only where it
communicates real runtime evidence.

## Development

```bash
npm install
npm run check
npm run dev
```

Run the Tauri shell with:

```bash
npm run tauri dev
```

Browser development falls back to a clearly identified design-preview snapshot.
The Tauri shell calls the native `runtime_snapshot` command and does not claim
Agent/Cloud readiness while the Runtime Manager is not attached.

## Current foundation scope

- Svelte 5 / Vite shell
- Tauri 2 native application boundary
- Overview / Agent / Sessions / Models / Updates / Diagnostics surfaces
- native-vs-preview data-source indicator
- typed `RuntimeSnapshot` UI contract
- cross-platform CI gates
- shared Rust Runtime Manager lives in `../hermes-runtime-manager/`

Next closure is the authenticated Desktop ↔ Runtime Manager IPC plus private
Python/toolchain activation.
