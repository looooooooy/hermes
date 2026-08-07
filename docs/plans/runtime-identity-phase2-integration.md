# Runtime Identity Phase 2 Integration

## Goal

Connect connector hello messages with Cloud runtime identity projection.

## Flow

connector.hello

-> RuntimeIdentityGatewayAdapter

-> RuntimeIdentityService

-> RuntimeIdentityRegistry

-> runtime active binding

## Next implementation targets

- connector hello transport integration
- cloud realtime projection
- runtime rollover events
- end-to-end verification
