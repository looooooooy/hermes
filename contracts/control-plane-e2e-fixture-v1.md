# Control Plane E2E Fixture v1

Scenario:

1. Create runtime descriptor
2. Register control extension
3. Bind connector
4. Send command from cloud
5. Validate runtime event creation
6. Validate effect receipt

Assertions:

- runtime identity remains unchanged
- stale generation is rejected
- duplicate command is idempotent
- effect receipt reaches terminal state
