# Owner Action Runtime Contract v1

## Purpose

Define the boundary for remote actions entering a Hermes runtime.

## Flow

Cloud Command

-> Connector

-> Control Extension

-> OwnerActionRouter

-> Runtime Authority

-> Internal Runtime Event

-> Agent Loop

## Rules

- Connector transports commands but does not execute Agent logic.
- Runtime generation must match before dispatch.
- Session authority remains inside Hermes runtime.
- Transport acknowledgement does not mean effect completion.

## Command Identity

Required fields:

- command_id
- runtime_generation
- session_id
- action
- requested_at

## Result States

- received
- validated
- dispatched
- accepted
- effect_started
- completed
- effect_unknown
