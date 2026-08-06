# Batch 12 Runtime Control Implementation Plan

## Goal

Move from control-plane contracts into the runtime execution boundary.

## Target flow

Cloud Command

-> Connector Dispatcher

-> Control Extension

-> OwnerActionRouter

-> Runtime Event Queue

-> Agent Runtime

-> Effect Receipt

## Implementation boundaries

### OwnerActionRouter

- Validate runtime generation
- Validate session binding
- Enforce idempotency using command_id
- Dispatch internal runtime events

### Runtime Event Queue

- Durable event envelope
- Runtime generation binding
- Session binding
- Processing state transitions

### Control Extension

- Adapter only
- No direct Agent invocation
- No direct SessionDB access

## Acceptance criteria

- Duplicate commands do not execute twice
- Stale runtime generations are rejected
- Effect receipts distinguish transport acknowledgement from runtime completion
- End-to-end command path is traceable by correlation id
