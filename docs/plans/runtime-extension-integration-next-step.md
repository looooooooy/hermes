# Runtime Extension Integration Checkpoint

## Current state

The plugin side now has:

- RuntimeBinding
- ExtensionRegistry
- Runtime health snapshot primitives
- Lifecycle coordination primitives

## Next implementation boundary

The next code change must connect the Host SPI adapter lifecycle to these primitives.

Required flow:

```
register_gateway_extension()
        |
        v
ExtensionLifecycleCoordinator
        |
        v
RuntimeBinding
        |
        v
Health Snapshot
        |
        v
Connector runtime verification
```

## Constraints

- Plugin must not own Hermes runtime lifecycle.
- Runtime identity must originate from the host runtime.
- Connector must verify runtime generation before accepting control traffic.
- Observer and control capabilities must be published from runtime state, not static configuration.
