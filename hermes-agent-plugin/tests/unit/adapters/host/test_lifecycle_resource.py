"""Lifecycle resource adapter migration tests."""

import threading
import time
from pathlib import Path

import pytest


class _Registration:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_host_lifecycle_adapter_owns_registration_cleanup() -> None:
    module_path = (
        Path(__file__).parents[4] / "src/hermes_agent_plugin/adapters/host/lifecycle.py"
    )
    assert module_path.is_file(), "canonical host lifecycle adapter is missing"

    from hermes_agent_plugin.adapters.host.lifecycle import (
        RelayEndpointResource,
    )

    registration = _Registration()
    resource = RelayEndpointResource(
        name="relay",
        starter=lambda: registration,
        clock=lambda: 10.0,
    )

    resource.start(11.0)
    resource.drain(11.0)
    resource.stop(11.0)
    resource.stop(11.0)

    assert registration.closed is True


def test_control_resource_owns_dispatcher_and_stops_while_action_is_blocked(
    monkeypatch,
) -> None:
    from hermes_agent_plugin.adapters.host import lifecycle

    captured: dict[str, object] = {}
    registration = _Registration()

    def start_control_endpoint(**kwargs):
        captured.update(kwargs)
        return registration

    monkeypatch.setattr(
        lifecycle,
        "start_control_endpoint",
        start_control_endpoint,
    )
    resource = lifecycle.control_relay_resource(
        authority=object(),
        dispatcher=lambda _request, _transport: None,
    )
    resource.start(time.monotonic() + 1)
    owner_action_dispatcher = captured.get("owner_action_dispatcher")
    assert owner_action_dispatcher is not None

    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        release.wait()

    future = owner_action_dispatcher.submit(block)
    assert future is not None
    assert started.wait(timeout=1)

    before = time.monotonic()
    try:
        resource.stop(time.monotonic() + 1)
        assert time.monotonic() - before < 0.5
        assert registration.closed is True
        assert owner_action_dispatcher.submit(lambda: None) is None
    finally:
        release.set()
        future.result(timeout=1)


def test_start_preserves_original_error_and_retries_both_failed_cleanups() -> None:
    from hermes_agent_plugin.adapters.host.lifecycle import (
        LifecycleDeadlineExceeded,
        RelayEndpointResource,
    )

    class RetryRegistration:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("registration close failed")

    class RetryDispatcher:
        def __init__(self) -> None:
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def submit(self, fn, /, *args, **kwargs):
            raise AssertionError("submit must not be called")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self.shutdown_calls.append((wait, cancel_futures))
            if len(self.shutdown_calls) == 1:
                raise RuntimeError("dispatcher shutdown failed")

    registration = RetryRegistration()
    dispatcher = RetryDispatcher()
    clock_values = iter((0.0, 2.0, 0.0))
    resource = RelayEndpointResource(
        name="control-relay",
        starter=lambda **_kwargs: registration,
        clock=lambda: next(clock_values),
        owner_action_dispatcher_factory=lambda: dispatcher,
    )

    with pytest.raises(
        LifecycleDeadlineExceeded,
        match="lifecycle_deadline_exceeded",
    ):
        resource.start(1.0)

    assert registration.close_calls == 1
    assert dispatcher.shutdown_calls == [(False, True)]
    assert resource._registration is registration
    assert resource._owner_action_dispatcher is dispatcher

    resource.stop(1.0)

    assert registration.close_calls == 2
    assert dispatcher.shutdown_calls == [(False, True), (False, True)]
    assert resource._registration is None
    assert resource._owner_action_dispatcher is None


def test_start_retains_only_registration_when_its_cleanup_keeps_failing() -> None:
    from hermes_agent_plugin.adapters.host.lifecycle import (
        LifecycleDeadlineExceeded,
        RelayEndpointResource,
    )

    class FailingRegistration:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("registration close failed")

    class RecordingDispatcher:
        def __init__(self) -> None:
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def submit(self, fn, /, *args, **kwargs):
            raise AssertionError("submit must not be called")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self.shutdown_calls.append((wait, cancel_futures))

    registration = FailingRegistration()
    dispatcher = RecordingDispatcher()
    clock_values = iter((0.0, 2.0))
    resource = RelayEndpointResource(
        name="control-relay",
        starter=lambda **_kwargs: registration,
        clock=lambda: next(clock_values),
        owner_action_dispatcher_factory=lambda: dispatcher,
    )

    with pytest.raises(
        LifecycleDeadlineExceeded,
        match="lifecycle_deadline_exceeded",
    ):
        resource.start(1.0)

    assert registration.close_calls == 1
    assert dispatcher.shutdown_calls == [(False, True)]
    assert resource._registration is registration
    assert resource._owner_action_dispatcher is None

    with pytest.raises(RuntimeError, match="registration close failed"):
        resource.stop(1.0)

    assert registration.close_calls == 2
    assert dispatcher.shutdown_calls == [(False, True)]
    assert resource._registration is registration
    assert resource._owner_action_dispatcher is None


def test_start_retains_only_dispatcher_when_its_cleanup_fails() -> None:
    from hermes_agent_plugin.adapters.host.lifecycle import (
        LifecycleDeadlineExceeded,
        RelayEndpointResource,
    )

    class RecordingRegistration:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class RetryDispatcher:
        def __init__(self) -> None:
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def submit(self, fn, /, *args, **kwargs):
            raise AssertionError("submit must not be called")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self.shutdown_calls.append((wait, cancel_futures))
            if len(self.shutdown_calls) == 1:
                raise RuntimeError("dispatcher shutdown failed")

    registration = RecordingRegistration()
    dispatcher = RetryDispatcher()
    clock_values = iter((0.0, 2.0, 0.0))
    resource = RelayEndpointResource(
        name="control-relay",
        starter=lambda **_kwargs: registration,
        clock=lambda: next(clock_values),
        owner_action_dispatcher_factory=lambda: dispatcher,
    )

    with pytest.raises(
        LifecycleDeadlineExceeded,
        match="lifecycle_deadline_exceeded",
    ):
        resource.start(1.0)

    assert registration.close_calls == 1
    assert dispatcher.shutdown_calls == [(False, True)]
    assert resource._registration is None
    assert resource._owner_action_dispatcher is dispatcher

    resource.stop(1.0)

    assert registration.close_calls == 1
    assert dispatcher.shutdown_calls == [(False, True), (False, True)]
    assert resource._registration is None
    assert resource._owner_action_dispatcher is None


def test_stop_preserves_registration_error_when_dispatcher_shutdown_also_fails() -> (
    None
):
    from hermes_agent_plugin.adapters.host.lifecycle import RelayEndpointResource

    class FailingRegistration:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("registration close failed")

    class FailingDispatcher:
        def __init__(self) -> None:
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def submit(self, fn, /, *args, **kwargs):
            raise AssertionError("submit must not be called")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self.shutdown_calls.append((wait, cancel_futures))
            raise RuntimeError("dispatcher shutdown failed")

    registration = FailingRegistration()
    dispatcher = FailingDispatcher()
    resource = RelayEndpointResource(
        name="control-relay",
        starter=lambda **_kwargs: registration,
        clock=lambda: 0.0,
        owner_action_dispatcher_factory=lambda: dispatcher,
    )
    resource.start(1.0)

    with pytest.raises(RuntimeError, match="registration close failed"):
        resource.stop(1.0)

    assert registration.close_calls == 1
    assert dispatcher.shutdown_calls == [(False, True)]


def test_stop_retains_failed_registration_for_a_second_cleanup_attempt() -> None:
    from hermes_agent_plugin.adapters.host.lifecycle import RelayEndpointResource

    class RetryRegistration:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("registration close failed")

    registration = RetryRegistration()
    resource = RelayEndpointResource(
        name="control-relay",
        starter=lambda: registration,
        clock=lambda: 0.0,
    )
    resource.start(1.0)

    with pytest.raises(RuntimeError, match="registration close failed"):
        resource.stop(1.0)

    resource.stop(1.0)
    assert registration.close_calls == 2
