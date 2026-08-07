# Runtime Control Plane v1

## Purpose

Define lifecycle state shared by Hermes Runtime, Connector and Cloud.

## States

```
STARTING
  -> DISCOVERING
  -> VERIFYING
  -> BOUND
  -> CONNECTING
  -> ACTIVE
  -> STALE
```

## Identity

Every runtime state must carry:

- runtime_id
- runtime_generation
- profile
- descriptor_hash

## Rule

Connector transport readiness does not imply runtime readiness.

Runtime ACTIVE requires verified identity binding.
