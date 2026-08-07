# Agent Runtime Control Implementation v1

## Flow

Cloud Command -> Connector -> Control Extension -> OwnerActionRouter -> Runtime Event Queue -> Agent Runtime

## Rules

- Connector transports only.
- Runtime owns execution authority.
- Every command carries runtime_generation and session identity.
- Effects require receipts.
