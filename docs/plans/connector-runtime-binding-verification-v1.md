# Connector Runtime Binding Verification v1

## Goal

Ensure Hermes Connector binds to the real Hermes Runtime instance instead of only a device identity.

## Runtime identity source

The Connector must consume Runtime Descriptor data produced by the Hermes Runtime authority.

Required fields:

- runtime_id
- runtime_generation
- profile
- extensions
- capabilities

## Verification flow

```text
Connector start
    |
    v
Discover local Runtime Descriptor
    |
    v
Validate runtime identity
    |
    v
Validate extension readiness
    |
    v
Connect Cloud
```

## Binding states

- UNKNOWN
- DISCOVERED
- VERIFIED
- ACTIVE
- STALE

## Rules

1. Connector must not generate runtime identity.
2. Connector must not infer profile from configuration when Runtime Descriptor is available.
3. Cloud registration should only happen after runtime verification succeeds.
4. Runtime generation change invalidates previous bindings.
