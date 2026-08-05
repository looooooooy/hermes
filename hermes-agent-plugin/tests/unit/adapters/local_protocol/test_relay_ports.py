"""Platform-neutral relay API delegates through an explicit backend port."""

from __future__ import annotations

from hermes_agent_plugin.adapters.local_protocol import (
    control_relay,
    observer_relay,
)


class _ControlHub:
    def call(self, request, **kwargs):
        return {"request": request, "kwargs": kwargs}

    def close_transport(self, transport):
        return int(transport == "downstream")


class _ObserverHub:
    def subscribe(self, session_key, profile, transport, *, runtime_generation):
        return {
            "session_key": session_key,
            "profile": profile,
            "transport": transport,
            "runtime_generation": runtime_generation,
        }

    def unsubscribe(self, subscription_id, transport):
        return subscription_id == "subscription" and transport == "downstream"

    def activate(self, subscription_id, transport):
        return subscription_id == "subscription" and transport == "downstream"

    def close_transport(self, transport):
        return int(transport == "downstream")


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def start_control_endpoint(self, **kwargs):
        self.calls.append(("start_control", kwargs))
        return "control-registration"

    def list_control_endpoints(self):
        self.calls.append(("list_control", {}))
        return ["control-endpoint"]

    def create_control_relay_hub(self, *, current_pid):
        self.calls.append(("create_control_hub", {"current_pid": current_pid}))
        return _ControlHub()

    def start_observer_endpoint(self, **kwargs):
        self.calls.append(("start_observer", kwargs))
        return "observer-registration"

    def list_observer_endpoints(self):
        self.calls.append(("list_observer", {}))
        return ["observer-endpoint"]

    def create_observer_relay_hub(self, *, current_pid):
        self.calls.append(("create_observer_hub", {"current_pid": current_pid}))
        return _ObserverHub()


def test_control_relay_api_delegates_to_injected_backend() -> None:
    backend = _Backend()
    authority = object()

    def dispatcher(_request, _transport):
        return None

    assert (
        control_relay.start_control_endpoint(
            authority=authority,
            dispatcher=dispatcher,
            backend=backend,
        )
        == "control-registration"
    )
    assert control_relay.list_control_endpoints(backend=backend) == ["control-endpoint"]
    hub = control_relay.ControlRelayHub(current_pid=42, backend=backend)
    assert hub.call(
        {"id": "request"},
        transport="downstream",
        auth_claims={"profile": "default"},
        profile="default",
    )["request"] == {"id": "request"}
    assert hub.close_transport("downstream") == 1

    assert backend.calls == [
        (
            "start_control",
            {
                "authority": authority,
                "dispatcher": dispatcher,
                "transport_cleanup": None,
            },
        ),
        ("list_control", {}),
        ("create_control_hub", {"current_pid": 42}),
    ]


def test_observer_relay_api_delegates_to_injected_backend() -> None:
    backend = _Backend()
    authority = object()

    def dispatch(_request, _transport):
        return None

    def cleanup(_transport):
        return None

    assert (
        observer_relay.start_observer_endpoint(
            authority=authority,
            dispatch=dispatch,
            remove_observer_subscriptions=cleanup,
            backend=backend,
        )
        == "observer-registration"
    )
    assert observer_relay.list_observer_endpoints(backend=backend) == [
        "observer-endpoint"
    ]
    hub = observer_relay.ObserverRelayHub(
        current_pid=42,
        backend=backend,
    )
    assert hub.subscribe(
        "session",
        "default",
        "downstream",
        runtime_generation="runtime-generation-1",
    ) == {
        "session_key": "session",
        "profile": "default",
        "transport": "downstream",
        "runtime_generation": "runtime-generation-1",
    }
    assert hub.activate("subscription", "downstream") is True
    assert hub.unsubscribe("subscription", "downstream") is True
    assert hub.close_transport("downstream") == 1

    assert backend.calls == [
        (
            "start_observer",
            {
                "authority": authority,
                "dispatch": dispatch,
                "remove_observer_subscriptions": cleanup,
            },
        ),
        ("list_observer", {}),
        ("create_observer_hub", {"current_pid": 42}),
    ]
