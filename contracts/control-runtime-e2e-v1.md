# Control Runtime E2E v1

## Flow

Cloud Command

```
Cloud
  -> Connector
  -> Control Extension
  -> OwnerActionRouter
  -> Runtime Authority
  -> Internal Runtime Event
  -> Agent Loop
```

## Rules

- Connector never executes Agent logic.
- Runtime generation must match before dispatch.
- Transport acknowledgement is not effect completion.
- Runtime owns session authority.
