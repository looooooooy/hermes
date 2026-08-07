# Runtime Command Ledger v1

States:

```
RECEIVED
VALIDATED
DISPATCHED
ACCEPTED
EFFECT_STARTED
COMPLETED
FAILED
```

A command is identified by command_id and runtime_generation.
Duplicate delivery must not create duplicate effects.
