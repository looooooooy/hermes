# Internal Runtime Event Queue v1

## Purpose

Provide an event boundary between control extensions and the Agent runtime.

## Event lifecycle

RECEIVED

-> VALIDATED

-> QUEUED

-> PROCESSING

-> COMPLETED

## Event identity

Required fields:

- event_id
- runtime_generation
- session_id
- event_type
- created_at

Remote callers never directly invoke the model loop.
