# Control Extension Adapter v1

## Responsibility

Bridge external commands into the Hermes runtime boundary.

## Not Responsible For

- Executing models directly
- Creating sessions
- Managing memory
- Writing SessionDB

## Flow

Control Extension

-> OwnerActionRouter

-> Runtime Authority

-> Internal Runtime Event

-> Agent Loop

## Required Validation

- runtime_generation matches
- session exists
- capability allows action
- command has idempotency key
