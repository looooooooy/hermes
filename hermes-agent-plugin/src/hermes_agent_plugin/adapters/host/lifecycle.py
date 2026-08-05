"""Adapters from existing relay registrations to lifecycle resource ports."""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import partial
from typing import Any

from ...domain.lifecycle import LifecycleDeadlineExceeded
from ...ports.local_relay import LocalRelayBackendPort, OwnerActionDispatcherPort
from ..local_protocol.control_relay import start_control_endpoint
from ..local_protocol.observer_relay import start_observer_endpoint
from .owner_actions import BoundedOwnerActionDispatcher


class RelayEndpointResource:
    """Own one existing relay endpoint registration per runtime generation."""

    def __init__(
        self,
        *,
        name: str,
        starter: Callable[[], Any],
        clock: Callable[[], float] = time.monotonic,
        owner_action_dispatcher_factory: (
            Callable[[], OwnerActionDispatcherPort] | None
        ) = None,
    ) -> None:
        self.name = name
        self._starter = starter
        self._clock = clock
        self._owner_action_dispatcher_factory = owner_action_dispatcher_factory
        self._owner_action_dispatcher: OwnerActionDispatcherPort | None = None
        self._registration: Any | None = None

    def _check_deadline(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise LifecycleDeadlineExceeded("lifecycle_deadline_exceeded")

    def start(self, deadline: float) -> None:
        if self._registration is not None:
            return
        self._check_deadline(deadline)
        owner_action_dispatcher: OwnerActionDispatcherPort | None = None
        registration: Any | None = None
        try:
            owner_action_dispatcher = (
                self._owner_action_dispatcher_factory()
                if self._owner_action_dispatcher_factory is not None
                else None
            )
            if owner_action_dispatcher is None:
                registration = self._starter()
            else:
                registration = self._starter(
                    owner_action_dispatcher=owner_action_dispatcher
                )
            self._check_deadline(deadline)
        except BaseException:
            try:
                if registration is not None:
                    registration.close()
            except BaseException:  # noqa: BLE001
                self._registration = registration
            try:
                if owner_action_dispatcher is not None:
                    owner_action_dispatcher.shutdown(
                        wait=False,
                        cancel_futures=True,
                    )
            except BaseException:  # noqa: BLE001
                self._owner_action_dispatcher = owner_action_dispatcher
            raise
        self._registration = registration
        self._owner_action_dispatcher = owner_action_dispatcher

    def drain(self, deadline: float) -> None:
        self._check_deadline(deadline)

    def stop(self, deadline: float) -> None:
        registration = self._registration
        owner_action_dispatcher = self._owner_action_dispatcher
        if registration is None and owner_action_dispatcher is None:
            return
        cleanup_error: BaseException | None = None
        registration_closed = registration is None
        dispatcher_closed = owner_action_dispatcher is None
        try:
            if registration is not None:
                registration.close()
                registration_closed = True
        except BaseException as error:  # noqa: BLE001
            cleanup_error = error
        try:
            if owner_action_dispatcher is not None:
                owner_action_dispatcher.shutdown(
                    wait=False,
                    cancel_futures=True,
                )
                dispatcher_closed = True
        except BaseException as error:  # noqa: BLE001
            cleanup_error = cleanup_error or error
        if registration_closed:
            self._registration = None
        if dispatcher_closed:
            self._owner_action_dispatcher = None
        if cleanup_error is not None:
            raise cleanup_error
        self._check_deadline(deadline)


def observer_relay_resource(
    *,
    authority: Any,
    dispatch: Callable[[dict[str, Any], Any], dict[str, Any] | None],
    remove_observer_subscriptions: Callable[[Any], None],
    backend: LocalRelayBackendPort | None = None,
) -> RelayEndpointResource:
    """Adapt the current observer endpoint without changing its wire."""
    return RelayEndpointResource(
        name="observer-relay",
        starter=partial(
            start_observer_endpoint,
            authority=authority,
            dispatch=dispatch,
            remove_observer_subscriptions=remove_observer_subscriptions,
            backend=backend,
        ),
    )


def control_relay_resource(
    *,
    authority: Any,
    dispatcher: Callable[[dict, Any], dict | None],
    transport_cleanup: Callable[[Any], None] | None = None,
    backend: LocalRelayBackendPort | None = None,
) -> RelayEndpointResource:
    """Adapt the current control endpoint without changing its wire."""
    return RelayEndpointResource(
        name="control-relay",
        starter=partial(
            start_control_endpoint,
            authority=authority,
            dispatcher=dispatcher,
            transport_cleanup=transport_cleanup,
            backend=backend,
        ),
        owner_action_dispatcher_factory=BoundedOwnerActionDispatcher,
    )
