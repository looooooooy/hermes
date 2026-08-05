# Runtime Binding E2E Contract v1

## Goal

Verify the full lifecycle:

Hermes Runtime -> Plugin Extension -> Connector -> Cloud Runtime Projection

## Lifecycle

1. Runtime creates descriptor
2. Connector discovers descriptor
3. Connector verifies identity
4. Connector sends handshake payload
5. Cloud creates runtime projection
6. Runtime becomes active

## Required Identity

- runtime_id
- runtime_generation
- profile
- descriptor_hash

## States

UNKNOWN -> DISCOVERED -> VERIFIED -> ACTIVE -> STALE
