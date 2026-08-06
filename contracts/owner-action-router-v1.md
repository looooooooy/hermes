# Owner Action Router v1

## Purpose

Define the boundary between remote control requests and the Hermes runtime.

## Flow

Cloud Command

-> Connector

-> Control Extension

-> OwnerActionRouter

-> Runtime Authority

-> Internal Runtime Event

-> Agent Loop

## Rules

- Connector transports commands only.
- Control Extension validates scope and runtime generation.
- Runtime Authority owns execution authority.
- Session ownership remains inside Runtime.
- Every action requires a command id for idempotency.
