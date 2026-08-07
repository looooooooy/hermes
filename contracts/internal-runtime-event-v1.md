# Internal Runtime Event Contract v1

## Purpose

Define the event boundary between Control Extension and Agent Runtime.

## Event

Fields:

- event_id
- runtime_generation
- session_id
- event_type
- payload

## Principle

External commands must become runtime events. They must not directly invoke model execution.

## Lifecycle

received -> queued -> processing -> completed
