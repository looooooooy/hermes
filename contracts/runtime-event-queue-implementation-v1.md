# Runtime Event Queue Implementation v1

## Purpose

Define the runtime-side execution boundary for remote commands.

## Flow

Cloud Command

-> Connector Dispatcher

-> Control Extension

-> OwnerActionRouter

-> Runtime Event Queue

-> Agent Runtime

## Rules

- Queue is the only entry point from remote control into the agent runtime.
- Connector MUST NOT invoke the agent loop directly.
- Every event MUST include:
  - event_id
  - command_id
  - runtime_generation
  - session_id
  - event_type

## States

RECEIVED

VALIDATED

QUEUED

PROCESSING

COMPLETED

FAILED

## Idempotency

Duplicate command_id values MUST resolve to the existing execution record.
