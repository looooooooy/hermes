# Remote Control E2E Scenario v1

Scenario:

1. Cloud creates command.
2. Connector receives command.
3. Runtime generation is verified.
4. Control extension dispatches action.
5. Runtime queues internal event.
6. Agent loop processes event.
7. Effect receipt is returned.

Failure cases:

- stale runtime generation
- duplicated command id
- missing session
- unavailable control capability
