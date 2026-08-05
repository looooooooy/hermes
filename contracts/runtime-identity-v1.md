# Runtime Identity Contract v1

## Purpose

Bind a connector to a real Hermes runtime instance.

## Required identity

- runtime_id
- runtime_generation
- profile
- descriptor_hash

## Lifecycle

UNKNOWN -> DISCOVERED -> VERIFIED -> ACTIVE -> STALE

## Rule

A connector being online does not imply that a runtime is active.
