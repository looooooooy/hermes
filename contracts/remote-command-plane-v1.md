# Remote Command Plane v1

## Purpose

Define the boundary between Cloud commands and the Hermes Runtime control loop.

## Flow

Cloud Command

-> Connector

-> Control Extension

-> Owner Action Router

-> Runtime Authority

-> Agent Loop

## Rules

- Connector does not execute Agent actions directly.
- Runtime generation must be verified before dispatch.
- Session authority remains inside Hermes Runtime.
- Transport acknowledgement is not effect confirmation.

## Lifecycle

RECEIVED

VALIDATED

DISPATCHED

ACCEPTED

EFFECT_STARTED

COMPLETED
