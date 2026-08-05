from __future__ import annotations

import gc
import inspect
import threading
import weakref
from collections.abc import Callable
from typing import Any

import pytest

from hermes_agent_plugin.adapters.host import extension as extension_module
from hermes_agent_plugin.adapters.host.extension import _ObserverWireController


class _Catalog:
    def __init__(self) -> None:
        self.close_transport_calls = 0
        self.close_calls = 0

    def close_transport(self, _transport: object) -> None:
        self.close_transport_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _Registration:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.close_calls = 0

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1


class _BlockingCloseRegistration(_Registration):
    def __init__(self) -> None:
        super().__init__()
        self.first_close_started = threading.Event()
        self.release_first_close = threading.Event()

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
            close_call = self.close_calls
        if close_call == 1:
            self.first_close_started.set()
            assert self.release_first_close.wait(timeout=2)


class _FailingCloseRegistration(_Registration):
    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
        raise RuntimeError("synthetic registration close failure")


class _Prepared:
    snapshot: object = {}

    def __init__(
        self,
        *,
        registration: _Registration | None = None,
        activate: Callable[[], object] | None = None,
    ) -> None:
        self.registration = registration or _Registration()
        self._activate = activate
        self.activate_calls = 0
        self.close_calls = 0

    def activate(self) -> object:
        self.activate_calls += 1
        if self._activate is not None:
            return self._activate()
        return self.registration

    def close(self) -> None:
        self.close_calls += 1


class _Adapter:
    def __init__(self, factory: Callable[[], _Prepared] | None = None) -> None:
        self._factory = factory or _Prepared
        self.prepared: list[_Prepared] = []

    def prepare(self, _params: dict[str, Any], _sink: object) -> _Prepared:
        prepared = self._factory()
        self.prepared.append(prepared)
        return prepared


class _BlockingPrepareAdapter(_Adapter):
    def __init__(self, *, blocked_prepares: int) -> None:
        super().__init__()
        self._blocked_prepares = blocked_prepares
        self._prepare_lock = threading.Lock()
        self.prepare_calls = 0
        self.blocked_prepares_started = threading.Event()
        self.release_prepares = threading.Event()

    def prepare(self, params: dict[str, Any], sink: object) -> _Prepared:
        with self._prepare_lock:
            self.prepare_calls += 1
            prepare_call = self.prepare_calls
            if prepare_call == self._blocked_prepares:
                self.blocked_prepares_started.set()
        if prepare_call <= self._blocked_prepares:
            assert self.release_prepares.wait(timeout=2)
        return super().prepare(params, sink)


class _Transport:
    def __init__(self, *, write_error: BaseException | None = None) -> None:
        self._write_error = write_error
        self._lock = threading.Lock()
        self.frames: list[dict[str, Any]] = []
        self.write_calls = 0
        self.disconnect_calls = 0

    def write(self, _frame: dict[str, Any]) -> bool:
        with self._lock:
            self.write_calls += 1
            self.frames.append(_frame)
        if self._write_error is not None:
            raise self._write_error
        return True

    def disconnect(self) -> None:
        with self._lock:
            self.disconnect_calls += 1


def _subscribe(
    controller: _ObserverWireController, transport: object, index: int
) -> None:
    controller.dispatch(
        {
            "jsonrpc": "2.0",
            "id": f"subscribe-{index}",
            "method": "session.observe.subscribe",
            "params": {},
        },
        transport,
    )


def _unsubscribe(
    controller: _ObserverWireController,
    transport: _Transport,
    subscription_id: str,
) -> None:
    controller.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "unsubscribe-1",
            "method": "session.observe.unsubscribe",
            "params": {"subscription_id": subscription_id},
        },
        transport,
    )


def _collect() -> None:
    for _ in range(3):
        gc.collect()


def test_disconnected_transport_churn_is_reclaimable_and_controller_state_is_bounded() -> (
    None
):
    controller = _ObserverWireController(_Adapter(), _Catalog())
    references: list[weakref.ReferenceType[_Transport]] = []

    for _ in range(10_000):
        transport = _Transport()
        references.append(weakref.ref(transport))
        controller.close_transport(transport)

    del transport
    _collect()

    assert sum(reference() is not None for reference in references) == 0
    assert not getattr(controller, "_transport_states", {})
    assert getattr(controller, "_reserved_subscriptions", 0) == 0


def test_controller_close_releases_active_transport_state() -> None:
    adapter = _Adapter()
    controller = _ObserverWireController(adapter, _Catalog())
    references: list[weakref.ReferenceType[_Transport]] = []

    for index in range(128):
        transport = _Transport()
        references.append(weakref.ref(transport))
        _subscribe(controller, transport, index)

    controller.close()
    del transport
    _collect()

    assert all(prepared.registration.close_calls == 1 for prepared in adapter.prepared)
    assert sum(reference() is not None for reference in references) == 0
    assert not getattr(controller, "_transport_states", {})
    assert getattr(controller, "_reserved_subscriptions", 0) == 0


def test_transport_close_during_blocked_activation_cannot_revive_subscription() -> None:
    activation_started = threading.Event()
    release_activation = threading.Event()
    registration = _Registration()

    def activate() -> object:
        activation_started.set()
        assert release_activation.wait(timeout=2)
        return registration

    adapter = _Adapter(lambda: _Prepared(registration=registration, activate=activate))
    controller = _ObserverWireController(adapter, _Catalog())
    transport = _Transport()
    reference = weakref.ref(transport)
    errors: list[BaseException] = []

    def run(target: object = transport) -> None:
        try:
            _subscribe(controller, target, 1)
        except BaseException as error:  # pragma: no cover - diagnostic capture
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert activation_started.wait(timeout=2)

    controller.close_transport(transport)
    controller.close_transport(transport)
    release_activation.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert registration.close_calls == 1
    assert not getattr(controller, "_active", {})
    assert not getattr(controller, "_transport_states", {})
    assert getattr(controller, "_reserved_subscriptions", 0) == 0

    del thread, transport, run
    _collect()
    assert reference() is None


def test_concurrent_subscribes_and_repeated_close_each_registration_once() -> None:
    release_activation = threading.Event()
    prepared_lock = threading.Lock()

    def factory() -> _Prepared:
        prepared = _Prepared()

        def activate() -> object:
            assert release_activation.wait(timeout=2)
            return prepared.registration

        prepared._activate = activate
        return prepared

    adapter = _Adapter(factory)
    controller = _ObserverWireController(adapter, _Catalog())
    transport = _Transport()
    reference = weakref.ref(transport)
    errors: list[BaseException] = []

    def run(index: int, target: object = transport) -> None:
        try:
            _subscribe(controller, target, index)
        except BaseException as error:  # pragma: no cover - diagnostic capture
            with prepared_lock:
                errors.append(error)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()

    for _ in range(200):
        with prepared_lock:
            if len(adapter.prepared) == len(threads) and all(
                prepared.activate_calls == 1 for prepared in adapter.prepared
            ):
                break
        threading.Event().wait(0.005)
    else:
        pytest.fail("concurrent subscriptions did not reach activation")

    controller.close_transport(transport)
    controller.close_transport(transport)
    release_activation.set()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert all(prepared.registration.close_calls == 1 for prepared in adapter.prepared)
    assert not getattr(controller, "_active", {})
    assert not getattr(controller, "_transport_states", {})
    assert getattr(controller, "_reserved_subscriptions", 0) == 0

    del threads, thread, transport, run
    _collect()
    assert reference() is None


@pytest.mark.parametrize("failure_point", ["write", "activate"])
def test_subscribe_exception_closes_once_and_releases_transport(
    failure_point: str,
) -> None:
    registration = _Registration()

    def activate() -> object:
        if failure_point == "activate":
            raise RuntimeError("synthetic activate failure")
        return registration

    adapter = _Adapter(lambda: _Prepared(registration=registration, activate=activate))
    controller = _ObserverWireController(adapter, _Catalog())
    transport = _Transport(
        write_error=(
            RuntimeError("synthetic write failure")
            if failure_point == "write"
            else None
        )
    )
    reference = weakref.ref(transport)

    with pytest.raises(RuntimeError, match=f"synthetic {failure_point} failure"):
        _subscribe(controller, transport, 1)

    controller.close_transport(transport)
    controller.close_transport(transport)

    assert adapter.prepared[0].close_calls == 1
    assert registration.close_calls == 0
    assert transport.disconnect_calls == 1
    assert not getattr(controller, "_transport_states", {})
    assert getattr(controller, "_reserved_subscriptions", 0) == 0

    del transport
    _collect()
    assert reference() is None


def test_transport_state_is_never_keyed_by_object_id() -> None:
    source = inspect.getsource(_ObserverWireController)

    assert "id(transport)" not in source


@pytest.mark.parametrize("concurrent_close", ["transport", "controller"])
def test_unsubscribe_and_concurrent_close_claim_registration_exactly_once(
    concurrent_close: str,
) -> None:
    registration = _BlockingCloseRegistration()
    adapter = _Adapter(lambda: _Prepared(registration=registration))
    controller = _ObserverWireController(adapter, _Catalog())
    transport = _Transport()
    _subscribe(controller, transport, 1)
    subscription_id = str(transport.frames[0]["result"]["subscription_id"])
    errors: list[BaseException] = []

    def unsubscribe() -> None:
        try:
            _unsubscribe(controller, transport, subscription_id)
        except BaseException as error:  # pragma: no cover - diagnostic capture
            errors.append(error)

    worker = threading.Thread(target=unsubscribe)
    worker.start()
    assert registration.first_close_started.wait(timeout=2)

    if concurrent_close == "transport":
        controller.close_transport(transport)
        controller.close_transport(transport)
    else:
        controller.close()
        controller.close()
    registration.release_first_close.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert registration.close_calls == 1
    assert not getattr(controller, "_transport_states", {})
    assert getattr(controller, "_reserved_subscriptions", 0) == 0


def test_failed_unsubscribe_close_is_not_retried_by_transport_cleanup() -> None:
    registration = _FailingCloseRegistration()
    adapter = _Adapter(lambda: _Prepared(registration=registration))
    controller = _ObserverWireController(adapter, _Catalog())
    transport = _Transport()
    _subscribe(controller, transport, 1)
    subscription_id = str(transport.frames[0]["result"]["subscription_id"])

    with pytest.raises(RuntimeError, match="synthetic registration close failure"):
        _unsubscribe(controller, transport, subscription_id)

    controller.close_transport(transport)
    controller.close_transport(transport)
    assert registration.close_calls == 1
    assert not getattr(controller, "_transport_states", {})
    assert getattr(controller, "_reserved_subscriptions", 0) == 0


def test_observer_subscription_capacity_defaults_are_frozen() -> None:
    assert extension_module.MAX_OBSERVER_SUBSCRIPTIONS_PER_TRANSPORT == 64
    assert extension_module.MAX_OBSERVER_SUBSCRIPTIONS_TOTAL == 1_024


def _start_blocked_subscriptions(
    controller: _ObserverWireController,
    transports: list[_Transport],
) -> tuple[list[threading.Thread], list[BaseException]]:
    errors: list[BaseException] = []

    def subscribe(index: int, transport: _Transport) -> None:
        try:
            _subscribe(controller, transport, index)
        except BaseException as error:  # pragma: no cover - diagnostic capture
            errors.append(error)

    workers = [
        threading.Thread(target=subscribe, args=(index, transport))
        for index, transport in enumerate(transports)
    ]
    for worker in workers:
        worker.start()
    return workers, errors


def test_per_transport_capacity_counts_in_flight_before_host_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extension_module,
        "MAX_OBSERVER_SUBSCRIPTIONS_PER_TRANSPORT",
        2,
        raising=False,
    )
    monkeypatch.setattr(
        extension_module,
        "MAX_OBSERVER_SUBSCRIPTIONS_TOTAL",
        8,
        raising=False,
    )
    adapter = _BlockingPrepareAdapter(blocked_prepares=2)
    controller = _ObserverWireController(adapter, _Catalog())
    transport = _Transport()
    workers, errors = _start_blocked_subscriptions(
        controller,
        [transport, transport],
    )
    assert adapter.blocked_prepares_started.wait(timeout=2)

    try:
        with pytest.raises(
            RuntimeError, match="observer subscription capacity exceeded"
        ):
            _subscribe(controller, transport, 3)
        assert adapter.prepare_calls == 2
        assert transport.write_calls == 0
    finally:
        adapter.release_prepares.set()
        for worker in workers:
            worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    with pytest.raises(RuntimeError, match="observer subscription capacity exceeded"):
        _subscribe(controller, transport, 4)
    assert adapter.prepare_calls == 2
    assert transport.write_calls == 2
    controller.close_transport(transport)
    assert not getattr(controller, "_transport_states", {})
    assert getattr(controller, "_reserved_subscriptions", 0) == 0


def test_global_capacity_counts_in_flight_across_transports_before_host_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extension_module,
        "MAX_OBSERVER_SUBSCRIPTIONS_PER_TRANSPORT",
        4,
        raising=False,
    )
    monkeypatch.setattr(
        extension_module,
        "MAX_OBSERVER_SUBSCRIPTIONS_TOTAL",
        2,
        raising=False,
    )
    adapter = _BlockingPrepareAdapter(blocked_prepares=2)
    controller = _ObserverWireController(adapter, _Catalog())
    first = _Transport()
    second = _Transport()
    rejected = _Transport()
    workers, errors = _start_blocked_subscriptions(controller, [first, second])
    assert adapter.blocked_prepares_started.wait(timeout=2)

    try:
        with pytest.raises(
            RuntimeError, match="observer subscription capacity exceeded"
        ):
            _subscribe(controller, rejected, 3)
        assert adapter.prepare_calls == 2
        assert rejected.write_calls == 0
    finally:
        adapter.release_prepares.set()
        for worker in workers:
            worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    with pytest.raises(RuntimeError, match="observer subscription capacity exceeded"):
        _subscribe(controller, rejected, 4)
    assert adapter.prepare_calls == 2
    assert rejected.write_calls == 0
    controller.close_transport(first)
    controller.close_transport(second)
    assert not getattr(controller, "_transport_states", {})
    assert getattr(controller, "_reserved_subscriptions", 0) == 0


def test_disconnect_releases_blocked_reservation_for_another_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extension_module,
        "MAX_OBSERVER_SUBSCRIPTIONS_PER_TRANSPORT",
        1,
        raising=False,
    )
    monkeypatch.setattr(
        extension_module,
        "MAX_OBSERVER_SUBSCRIPTIONS_TOTAL",
        1,
        raising=False,
    )
    adapter = _BlockingPrepareAdapter(blocked_prepares=1)
    controller = _ObserverWireController(adapter, _Catalog())
    blocked = _Transport()
    replacement = _Transport()
    workers, errors = _start_blocked_subscriptions(controller, [blocked])
    assert adapter.blocked_prepares_started.wait(timeout=2)

    controller.close_transport(blocked)
    _subscribe(controller, replacement, 2)
    adapter.release_prepares.set()
    workers[0].join(timeout=2)
    controller.close_transport(replacement)

    assert not workers[0].is_alive()
    assert errors == []
    assert getattr(controller, "_reserved_subscriptions", 0) == 0
