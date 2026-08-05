"""Session Catalog v1 over the persistent Observer-role transport."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from tests.test_support.host_spi_v1 import SessionCatalogRequest

from hermes_agent_plugin.adapters.local_protocol import session_catalog_v1 as catalog_v1
from hermes_agent_plugin.adapters.local_protocol.session_catalog_v1 import (
    SessionCatalogV1Controller as _SessionCatalogV1Controller,
)
from hermes_agent_plugin.adapters.local_protocol.session_catalog_v1 import (
    SessionCatalogV1Violation,
    load_session_catalog_v1_bundle,
)

CONTRACTS_ROOT = Path(__file__).resolve().parents[5] / "contracts"


def _generated_json(path: str) -> dict[str, object]:
    value = json.loads(
        resources.files("hermes_agent_plugin.contracts.generated")
        .joinpath(path)
        .read_text(encoding="utf-8")
    )
    assert isinstance(value, dict)
    return value


def _rpc_validator() -> Draft202012Validator:
    entry = _generated_json("schemas/session-catalog-entry-v1.schema.json")
    registry = Registry().with_resource(
        str(entry["$id"]),
        Resource.from_contents(entry),
    )
    return Draft202012Validator(
        _generated_json("schemas/local/session-catalog-rpc-v1.schema.json"),
        format_checker=FormatChecker(),
        registry=registry,
    )


RPC_VALIDATOR = _rpc_validator()


def SessionCatalogV1Controller(**kwargs: object) -> _SessionCatalogV1Controller:
    return _SessionCatalogV1Controller(lock=threading.RLock(), **kwargs)


def test_runtime_loads_the_frozen_generated_catalog_contract() -> None:
    bundle = load_session_catalog_v1_bundle()

    assert bundle.capability == "session.catalog.v1"
    assert bundle.page_size_maximum == 128
    assert bundle.event_buffer_maximum == 1_024
    assert bundle.frame_maximum_utf8_bytes == 262_144


def test_runtime_contract_load_fails_closed_on_unsupported_schema_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources_by_path = {
        "session-catalog-v1.json": _generated_json("session-catalog-v1.json"),
        "schemas/session-catalog-entry-v1.schema.json": _generated_json(
            "schemas/session-catalog-entry-v1.schema.json"
        ),
        "schemas/local/session-catalog-rpc-v1.schema.json": _generated_json(
            "schemas/local/session-catalog-rpc-v1.schema.json"
        ),
    }
    resources_by_path["schemas/local/session-catalog-rpc-v1.schema.json"][
        "unevaluatedProperties"
    ] = False
    monkeypatch.setattr(
        catalog_v1,
        "_read_generated_json",
        resources_by_path.__getitem__,
    )

    with pytest.raises(SessionCatalogV1Violation, match="resources drifted"):
        load_session_catalog_v1_bundle()


def _fixture(name: str) -> dict[str, object]:
    value = json.loads(
        (CONTRACTS_ROOT / "fixtures" / "valid" / name).read_text(encoding="utf-8")
    )
    assert isinstance(value, dict)
    return value


class _Registration:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _Host:
    def __init__(self, pages: list[object]) -> None:
        self.pages = list(pages)
        self.requests: list[object] = []
        self.listener: Callable[[object], None] | None = None
        self.listener_registration = _Registration()
        self.listener_registrations: list[_Registration] = []
        self.operations: list[str] = []

    def add_session_catalog_listener(self, listener: object) -> _Registration:
        assert callable(listener)
        self.operations.append("listener")
        self.listener = listener
        registration = (
            self.listener_registration
            if not self.listener_registrations
            else _Registration()
        )
        self.listener_registrations.append(registration)
        return registration

    def session_catalog(self, request: object) -> object:
        self.operations.append("page")
        self.requests.append(request)
        return self.pages.pop(0)


class _Binding:
    def supports_version(self, capability: str, version: int) -> bool:
        return (capability, version) == ("session.catalog.v1", 1)

    def require(self, *, profile: object, runtime_generation: object) -> None:
        if (profile, runtime_generation) != ("default", "runtime-20260803-01"):
            raise RuntimeError("binding mismatch")

    def matches(self, *, profile: str, runtime_generation: str) -> bool:
        return (profile, runtime_generation) == (
            "default",
            "runtime-20260803-01",
        )


class _Transport:
    def __init__(self, *, on_first_write: Callable[[], None] | None = None) -> None:
        self.frames: list[dict[str, object]] = []
        self.disconnect_calls = 0
        self.on_first_write = on_first_write

    def write(self, frame: dict[str, object]) -> bool:
        RPC_VALIDATOR.validate(frame)
        self.frames.append(frame)
        callback = self.on_first_write
        self.on_first_write = None
        if callback is not None:
            callback()
        return True

    def disconnect(self) -> None:
        self.disconnect_calls += 1


def _page(
    *, next_cursor: str | None, sessions: tuple[object, ...] | None = None
) -> object:
    return SimpleNamespace(
        profile="default",
        runtime_generation="runtime-20260803-01",
        catalog_revision=7,
        sessions=(
            (
                SimpleNamespace(
                    profile="default",
                    durable_session_key="durable-session-real",
                    runtime_generation="runtime-20260803-01",
                    surface="gateway",
                    authority_revision=3,
                    available_actions=frozenset({"prompt.submit"}),
                ),
            )
            if sessions is None
            else sessions
        ),
        next_cursor=next_cursor,
    )


def _page_with_revision(
    *,
    revision: int,
    next_cursor: str | None,
    sessions: tuple[object, ...],
) -> object:
    page = _page(next_cursor=next_cursor, sessions=sessions)
    page.catalog_revision = revision
    return page


def _ids(*values: str) -> Callable[[], str]:
    pending = iter(values)
    return lambda: next(pending)


def _event(
    *,
    sequence: int,
    session_key: str = "durable-session-new",
    action: str = "upsert",
) -> object:
    return SimpleNamespace(
        profile="default",
        runtime_generation="runtime-20260803-01",
        sequence=sequence,
        action=action,
        entry=SimpleNamespace(
            profile="default",
            durable_session_key=session_key,
            runtime_generation="runtime-20260803-01",
            surface="cli",
            authority_revision=1,
            available_actions=frozenset({"prompt.submit", "session.interrupt"}),
        ),
    )


def test_subscribe_registers_listener_first_and_writes_exact_first_page() -> None:
    host = _Host([_page(next_cursor="local-opaque-cursor")])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )

    result = controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )

    assert result is None
    assert host.operations == ["listener", "page"]
    assert host.requests == [
        SessionCatalogRequest(
            profile="default",
            runtime_generation="runtime-20260803-01",
            page_size=128,
            cursor=None,
        )
    ]
    assert transport.frames == [_fixture("session-catalog-local-subscribe-result.json")]
    assert host.listener_registration.close_calls == 0
    assert transport.disconnect_calls == 0


def test_page_uses_only_the_opaque_host_cursor_and_accepts_an_empty_final_page() -> (
    None
):
    host = _Host(
        [
            _page(next_cursor="local-opaque-cursor"),
            _page(next_cursor=None, sessions=()),
        ]
    )
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )

    result = controller.dispatch(
        _fixture("session-catalog-local-page.json"),
        transport,
    )

    assert result is None
    assert host.requests[-1] == SessionCatalogRequest(
        profile="default",
        runtime_generation="runtime-20260803-01",
        page_size=128,
        cursor="local-opaque-cursor",
    )
    assert transport.frames[-1] == _fixture("session-catalog-local-page-result.json")


def test_final_page_write_commits_snapshot_before_flushing_buffered_event() -> None:
    host = _Host([_page(next_cursor=None)])
    transport = _Transport(
        on_first_write=lambda: host.listener(_event(sequence=8)),  # type: ignore[misc]
    )
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )

    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )

    expected_snapshot = _fixture("session-catalog-local-subscribe-result.json")
    expected_snapshot["result"]["is_last"] = True  # type: ignore[index]
    expected_snapshot["result"]["next_cursor"] = None  # type: ignore[index]
    assert transport.frames == [
        expected_snapshot,
        _fixture("session-catalog-local-event.json"),
    ]


def test_changed_revision_returns_exact_error_and_closes_only_the_subscription() -> (
    None
):
    host = _Host(
        [
            _page(next_cursor="local-opaque-cursor"),
            _page_with_revision(revision=8, next_cursor=None, sessions=()),
        ]
    )
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )

    controller.dispatch(
        _fixture("session-catalog-local-page.json"),
        transport,
    )

    expected_error = _fixture("session-catalog-local-error-response.json")
    expected_error["id"] = "44444444-4444-4444-8444-444444444444"
    assert transport.frames[-1] == expected_error
    assert host.listener_registration.close_calls == 1
    assert transport.disconnect_calls == 0


def test_stale_host_cursor_returns_body_free_exact_error() -> None:
    class StaleCursorHost(_Host):
        def session_catalog(self, request: object) -> object:
            if getattr(request, "cursor", None) is not None:
                raise RuntimeError("secret cursor implementation detail")
            return super().session_catalog(request)

    host = StaleCursorHost([_page(next_cursor="local-opaque-cursor")])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )

    controller.dispatch(
        _fixture("session-catalog-local-page.json"),
        transport,
    )

    assert transport.frames[-1] == {
        "jsonrpc": "2.0",
        "id": "44444444-4444-4444-8444-444444444444",
        "error": {
            "code": 4400,
            "message": "session catalog reset required",
            "reason": "cursor_stale",
        },
    }
    assert "secret" not in json.dumps(transport.frames)
    assert host.listener_registration.close_calls == 1


def test_unsubscribe_is_idempotent_and_closes_the_listener_once() -> None:
    host = _Host([_page(next_cursor=None)])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )
    unsubscribe = _fixture("session-catalog-local-unsubscribe.json")

    controller.dispatch(unsubscribe, transport)
    repeated = dict(unsubscribe)
    repeated["id"] = "66666666-6666-4666-8666-666666666666"
    controller.dispatch(repeated, transport)

    assert transport.frames[-2:] == [
        {
            "jsonrpc": "2.0",
            "id": "55555555-5555-4555-8555-555555555555",
            "result": {
                "subscription_id": "22222222-2222-4222-8222-222222222222",
                "closed": True,
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "66666666-6666-4666-8666-666666666666",
            "result": {
                "subscription_id": "22222222-2222-4222-8222-222222222222",
                "closed": True,
            },
        },
    ]
    assert host.listener_registration.close_calls == 1


def test_unknown_unsubscribe_does_not_close_an_unrelated_active_subscription() -> None:
    host = _Host([_page(next_cursor=None)])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )
    unsubscribe = _fixture("session-catalog-local-unsubscribe.json")
    unsubscribe["params"][  # type: ignore[index]
        "subscription_id"
    ] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    controller.dispatch(unsubscribe, transport)

    assert transport.frames[-1] == {
        "jsonrpc": "2.0",
        "id": "55555555-5555-4555-8555-555555555555",
        "error": {
            "code": 4400,
            "message": "session catalog reset required",
            "reason": "transport_replaced",
        },
    }
    assert host.listener_registration.close_calls == 0
    assert host.listener is not None
    host.listener(_event(sequence=8))
    assert transport.frames[-1]["method"] == "session.catalog.event"


def test_staging_buffer_overflow_sends_reset_and_closes_subscription() -> None:
    host = _Host([_page(next_cursor="local-opaque-cursor")])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )
    assert host.listener is not None

    for _ in range(1_025):
        host.listener(_event(sequence=8))

    expected = _fixture("session-catalog-local-reset-required.json")
    expected["params"]["reason"] = "buffer_overflow"  # type: ignore[index]
    assert transport.frames[-1] == expected
    assert host.listener_registration.close_calls == 1


def test_transport_disconnect_clears_state_and_fences_late_callback() -> None:
    host = _Host([_page(next_cursor="local-opaque-cursor")])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )
    assert host.listener is not None
    stale_listener = host.listener

    controller.close_transport(transport)
    stale_listener(_event(sequence=8))

    assert len(transport.frames) == 1
    assert host.listener_registration.close_calls == 1


def test_runtime_rollover_notifies_and_fences_old_generation_callback() -> None:
    host = _Host([_page(next_cursor=None)])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )
    assert host.listener is not None
    stale_listener = host.listener

    controller.rollover()
    stale_listener(_event(sequence=8))

    expected = _fixture("session-catalog-local-reset-required.json")
    expected["params"]["reason"] = "runtime_generation_changed"  # type: ignore[index]
    assert transport.frames[-1] == expected
    assert len(transport.frames) == 2
    assert host.listener_registration.close_calls == 1


def test_new_subscribe_replaces_old_transport_subscription_before_host_read() -> None:
    host = _Host(
        [
            _page(next_cursor="local-opaque-cursor"),
            _page(next_cursor=None),
        ]
    )
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
            "77777777-7777-4777-8777-777777777777",
            "88888888-8888-4888-8888-888888888888",
        ),
    )
    first_request = _fixture("session-catalog-local-subscribe.json")
    controller.dispatch(first_request, transport)
    assert host.listener is not None
    old_listener = host.listener
    second_request = dict(first_request)
    second_request["id"] = "99999999-9999-4999-8999-999999999999"

    controller.dispatch(second_request, transport)
    old_listener(_event(sequence=8))

    expected_reset = _fixture("session-catalog-local-reset-required.json")
    expected_reset["params"]["reason"] = "transport_replaced"  # type: ignore[index]
    assert transport.frames[1] == expected_reset
    assert transport.frames[2]["id"] == second_request["id"]
    assert (
        transport.frames[2]["result"]["subscription_id"]  # type: ignore[index]
        == "77777777-7777-4777-8777-777777777777"
    )
    assert host.listener_registrations[0].close_calls == 1
    assert len(transport.frames) == 3


def test_final_page_write_failure_never_flushes_buffered_events() -> None:
    host = _Host([_page(next_cursor=None)])

    class FailingTransport(_Transport):
        def write(self, frame: dict[str, object]) -> bool:
            super().write(frame)
            return False

    transport = FailingTransport(
        on_first_write=lambda: host.listener(_event(sequence=8)),  # type: ignore[misc]
    )
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )

    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )

    assert len(transport.frames) == 1
    assert host.listener_registration.close_calls == 1
    assert transport.disconnect_calls == 1


def test_page_cursor_mismatch_returns_cursor_stale_and_closes_subscription() -> None:
    host = _Host([_page(next_cursor="local-opaque-cursor")])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )
    page_request = _fixture("session-catalog-local-page.json")
    page_request["params"]["cursor"] = "wrong-opaque-cursor"  # type: ignore[index]

    controller.dispatch(page_request, transport)

    assert transport.frames[-1] == {
        "jsonrpc": "2.0",
        "id": "44444444-4444-4444-8444-444444444444",
        "error": {
            "code": 4400,
            "message": "session catalog reset required",
            "reason": "cursor_stale",
        },
    }
    assert len(host.requests) == 1
    assert host.listener_registration.close_calls == 1


def test_initial_host_page_exception_returns_safe_revision_error() -> None:
    class FailingHost(_Host):
        def session_catalog(self, request: object) -> object:
            raise RuntimeError("token=secret internal Host failure")

    host = FailingHost([])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )

    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )

    assert transport.frames == [
        {
            "jsonrpc": "2.0",
            "id": "11111111-1111-4111-8111-111111111111",
            "error": {
                "code": 4400,
                "message": "session catalog reset required",
                "reason": "page_revision_changed",
            },
        }
    ]
    assert "secret" not in json.dumps(transport.frames)
    assert host.listener_registration.close_calls == 1
    assert transport.disconnect_calls == 0


def test_live_event_gap_sends_reset_instead_of_forwarding_event() -> None:
    host = _Host([_page(next_cursor=None)])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )
    assert host.listener is not None

    host.listener(_event(sequence=9))

    expected = _fixture("session-catalog-local-reset-required.json")
    expected["params"]["reason"] = "event_gap"  # type: ignore[index]
    assert transport.frames[-1] == expected
    assert host.listener_registration.close_calls == 1


def test_remove_event_forwards_the_complete_canonical_entry() -> None:
    host = _Host([_page(next_cursor=None)])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )
    assert host.listener is not None

    host.listener(_event(sequence=8, action="remove"))

    assert transport.frames[-1] == {
        "jsonrpc": "2.0",
        "method": "session.catalog.event",
        "params": {
            "subscription_id": "22222222-2222-4222-8222-222222222222",
            "profile": "default",
            "runtime_generation": "runtime-20260803-01",
            "catalog_sequence": 8,
            "action": "remove",
            "entry": {
                "session_key": "durable-session-new",
                "surface": "cli",
                "authority_revision": 1,
                "available_actions": [
                    "prompt.submit",
                    "session.interrupt",
                ],
            },
        },
    }


def test_listener_event_already_in_snapshot_revision_is_not_replayed() -> None:
    class RacingHost(_Host):
        def session_catalog(self, request: object) -> object:
            assert self.listener is not None
            self.listener(_event(sequence=7))
            return super().session_catalog(request)

    host = RacingHost([_page(next_cursor=None)])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )

    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )

    assert len(transport.frames) == 1
    assert host.listener_registration.close_calls == 0


def test_concurrent_page_request_resets_once_and_fences_inflight_result() -> None:
    class BlockingPageHost(_Host):
        def __init__(self) -> None:
            super().__init__([_page(next_cursor="local-opaque-cursor")])
            self.page_started = threading.Event()
            self.release_page = threading.Event()
            self.cursor_calls = 0

        def session_catalog(self, request: object) -> object:
            if getattr(request, "cursor", None) is None:
                return super().session_catalog(request)
            self.cursor_calls += 1
            if self.cursor_calls == 1:
                self.page_started.set()
                assert self.release_page.wait(timeout=1)
            return _page(next_cursor=None, sessions=())

    host = BlockingPageHost()
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(
        _fixture("session-catalog-local-subscribe.json"),
        transport,
    )
    page_request = _fixture("session-catalog-local-page.json")
    worker_errors: list[BaseException] = []

    def request_page() -> None:
        try:
            controller.dispatch(page_request, transport)
        except BaseException as error:  # noqa: BLE001
            worker_errors.append(error)

    worker = threading.Thread(target=request_page)
    worker.start()
    assert host.page_started.wait(timeout=1)

    controller.dispatch(page_request, transport)
    host.release_page.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert worker_errors == []
    assert host.cursor_calls == 1
    assert len(transport.frames) == 2
    assert transport.frames[-1]["error"]["reason"] == "cursor_stale"  # type: ignore[index]
    assert host.listener_registration.close_calls == 1


@pytest.mark.parametrize("failure", ("raise", "none", "non_bool"))
@pytest.mark.parametrize(
    "path",
    ("initial", "page", "event", "reset", "error", "unsubscribe"),
)
def test_every_transport_write_failure_closes_disconnects_and_never_escapes(
    path: str,
    failure: str,
) -> None:
    class FailingWriteTransport(_Transport):
        def __init__(self, fail_call: int) -> None:
            super().__init__()
            self.fail_call = fail_call
            self.write_calls = 0

        def write(self, frame: dict[str, object]) -> object:
            RPC_VALIDATOR.validate(frame)
            self.frames.append(frame)
            self.write_calls += 1
            if self.write_calls != self.fail_call:
                return True
            if failure == "raise":
                raise RuntimeError("secret transport failure")
            if failure == "none":
                return None
            return 1

    pages = (
        [_page(next_cursor="local-opaque-cursor"), _page(next_cursor=None, sessions=())]
        if path in {"page", "error"}
        else [_page(next_cursor=None)]
    )
    host = _Host(pages)
    transport = FailingWriteTransport(fail_call=1 if path == "initial" else 2)
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )

    controller.dispatch(_fixture("session-catalog-local-subscribe.json"), transport)
    if path == "page":
        controller.dispatch(_fixture("session-catalog-local-page.json"), transport)
    elif path == "event":
        assert host.listener is not None
        host.listener(_event(sequence=8))
    elif path == "reset":
        assert host.listener is not None
        host.listener(_event(sequence=9))
    elif path == "error":
        request = _fixture("session-catalog-local-page.json")
        request["params"]["cursor"] = "wrong-cursor"  # type: ignore[index]
        controller.dispatch(request, transport)
    elif path == "unsubscribe":
        controller.dispatch(
            _fixture("session-catalog-local-unsubscribe.json"), transport
        )

    controller.close_transport(transport)
    assert host.listener_registration.close_calls == 1
    assert transport.disconnect_calls == 1
    assert "secret transport failure" not in json.dumps(transport.frames)


@pytest.mark.parametrize("reset_kind", ("event_gap", "buffer_overflow"))
def test_concurrent_reset_sources_emit_one_terminal_frame_and_close_once(
    reset_kind: str,
) -> None:
    class BlockingResetTransport(_Transport):
        def __init__(self) -> None:
            super().__init__()
            self.reset_started = threading.Event()
            self.release_reset = threading.Event()
            self._frames_lock = threading.Lock()

        def write(self, frame: dict[str, object]) -> bool:
            RPC_VALIDATOR.validate(frame)
            if frame.get("method") == "session.catalog.reset_required":
                self.reset_started.set()
                assert self.release_reset.wait(timeout=1)
            with self._frames_lock:
                self.frames.append(frame)
            return True

    host = _Host(
        [
            _page(
                next_cursor=(
                    "local-opaque-cursor" if reset_kind == "buffer_overflow" else None
                )
            )
        ]
    )
    transport = BlockingResetTransport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(_fixture("session-catalog-local-subscribe.json"), transport)
    assert host.listener is not None
    if reset_kind == "buffer_overflow":
        for _ in range(1_024):
            host.listener(_event(sequence=8))
    errors: list[BaseException] = []

    def trigger(sequence: int) -> None:
        try:
            assert host.listener is not None
            host.listener(_event(sequence=sequence))
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    first = threading.Thread(target=trigger, args=(9,))
    second = threading.Thread(target=trigger, args=(10,))
    first.start()
    assert transport.reset_started.wait(timeout=1)
    second.start()
    second.join(timeout=0.2)
    transport.release_reset.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert errors == []
    assert not first.is_alive()
    assert not second.is_alive()
    reset_frames = [
        frame
        for frame in transport.frames
        if frame.get("method") == "session.catalog.reset_required"
    ]
    assert len(reset_frames) == 1
    assert host.listener_registration.close_calls == 1


def test_duplicate_available_actions_never_emit_an_invalid_page() -> None:
    duplicate_entry = SimpleNamespace(
        profile="default",
        durable_session_key="durable-session-real",
        runtime_generation="runtime-20260803-01",
        surface="gateway",
        authority_revision=3,
        available_actions=["prompt.submit", "prompt.submit"],
    )
    host = _Host([_page(next_cursor=None, sessions=(duplicate_entry,))])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )

    controller.dispatch(_fixture("session-catalog-local-subscribe.json"), transport)

    assert len(transport.frames) == 1
    assert transport.frames[0]["error"]["reason"] == "page_revision_changed"  # type: ignore[index]
    assert host.listener_registration.close_calls == 1


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("surface", ""),
        ("authority_revision", 0),
        ("durable_session_key", ""),
    ),
)
def test_invalid_dynamic_host_entry_never_reaches_the_transport(
    field: str,
    invalid: object,
) -> None:
    entry = SimpleNamespace(
        profile="default",
        durable_session_key="durable-session-real",
        runtime_generation="runtime-20260803-01",
        surface="gateway",
        authority_revision=3,
        available_actions=["prompt.submit"],
    )
    setattr(entry, field, invalid)
    host = _Host([_page(next_cursor=None, sessions=(entry,))])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )

    controller.dispatch(_fixture("session-catalog-local-subscribe.json"), transport)

    assert len(transport.frames) == 1
    assert "result" not in transport.frames[0]
    assert transport.frames[0]["error"]["reason"] == "page_revision_changed"  # type: ignore[index]
    assert host.listener_registration.close_calls == 1


@pytest.mark.parametrize(
    ("target", "field", "invalid"),
    (
        ("entry", "available_actions", ["prompt.submit", "prompt.submit"]),
        ("entry", "surface", ""),
        ("entry", "authority_revision", 0),
        ("entry", "session_key", ""),
        ("entry", "extra", "forbidden"),
        ("frame", "extra", "forbidden"),
    ),
)
def test_runtime_output_validator_rejects_dynamic_schema_violations(
    target: str,
    field: str,
    invalid: object,
) -> None:
    frame = _fixture("session-catalog-local-subscribe-result.json")
    mutation = frame if target == "frame" else frame["result"]["sessions"][0]  # type: ignore[index]
    mutation[field] = invalid  # type: ignore[index]

    with pytest.raises(SessionCatalogV1Violation):
        load_session_catalog_v1_bundle().validate_output(frame)


def test_rollover_fence_concurrent_page_and_complete_emit_one_terminal_frame() -> None:
    host = _Host([_page(next_cursor="local-opaque-cursor")])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(_fixture("session-catalog-local-subscribe.json"), transport)
    fence = controller.fence_rollover()
    start = threading.Barrier(3)
    errors: list[BaseException] = []

    def page() -> None:
        try:
            start.wait(timeout=1)
            controller.dispatch(_fixture("session-catalog-local-page.json"), transport)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    def complete() -> None:
        try:
            start.wait(timeout=1)
            controller.complete_rollover(fence)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    page_worker = threading.Thread(target=page)
    complete_worker = threading.Thread(target=complete)
    page_worker.start()
    complete_worker.start()
    start.wait(timeout=1)
    page_worker.join(timeout=1)
    complete_worker.join(timeout=1)

    assert errors == []
    terminal_frames = [
        frame
        for frame in transport.frames[1:]
        if "error" in frame or frame.get("method") == "session.catalog.reset_required"
    ]
    expected_reset = _fixture("session-catalog-local-reset-required.json")
    expected_reset["params"]["reason"] = "runtime_generation_changed"  # type: ignore[index]
    assert terminal_frames == [expected_reset]
    assert host.listener_registration.close_calls == 1


def test_page_for_a_tombstoned_subscription_is_silently_fenced() -> None:
    host = _Host([_page(next_cursor="local-opaque-cursor")])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    controller.dispatch(_fixture("session-catalog-local-subscribe.json"), transport)
    controller.dispatch(_fixture("session-catalog-local-unsubscribe.json"), transport)
    frames_before_page = list(transport.frames)

    controller.dispatch(_fixture("session-catalog-local-page.json"), transport)

    assert transport.frames == frames_before_page
    assert host.listener_registration.close_calls == 1


def test_tombstone_window_is_bounded_replay_safe_and_cleared_on_disconnect() -> None:
    limit = catalog_v1.MAX_CLOSED_SUBSCRIPTION_TOMBSTONES

    def canonical_uuid(index: int) -> str:
        return str(uuid.UUID(f"00000000-0000-4000-8000-{index + 1:012x}"))

    closed_ids = [canonical_uuid(index) for index in range(limit + 1)]
    new_active_id = canonical_uuid(limit + 10)
    snapshots = [canonical_uuid(limit + 100 + index) for index in range(limit + 3)]
    generated_ids: list[str] = []
    for subscription_id, snapshot_id in zip(
        [*closed_ids, new_active_id, closed_ids[0]],
        snapshots,
        strict=True,
    ):
        generated_ids.extend((subscription_id, snapshot_id))
    host = _Host([_page(next_cursor=None) for _ in snapshots])
    transport = _Transport()
    controller = SessionCatalogV1Controller(
        host=host,
        binding=_Binding(),
        request_factory=SessionCatalogRequest,
        id_factory=_ids(*generated_ids),
    )

    for subscription_id in closed_ids:
        controller.dispatch(_fixture("session-catalog-local-subscribe.json"), transport)
        unsubscribe = _fixture("session-catalog-local-unsubscribe.json")
        unsubscribe["params"]["subscription_id"] = subscription_id  # type: ignore[index]
        controller.dispatch(unsubscribe, transport)

    latest_registration = host.listener_registrations[-1]
    repeated = _fixture("session-catalog-local-unsubscribe.json")
    repeated["params"]["subscription_id"] = closed_ids[-1]  # type: ignore[index]
    controller.dispatch(repeated, transport)
    assert transport.frames[-1]["result"]["closed"] is True  # type: ignore[index]
    assert latest_registration.close_calls == 1

    controller.dispatch(_fixture("session-catalog-local-subscribe.json"), transport)
    new_registration = host.listener_registrations[-1]
    evicted = _fixture("session-catalog-local-unsubscribe.json")
    evicted["params"]["subscription_id"] = closed_ids[0]  # type: ignore[index]
    controller.dispatch(evicted, transport)
    assert transport.frames[-1]["error"]["reason"] == "transport_replaced"  # type: ignore[index]
    assert new_registration.close_calls == 0

    close_new = _fixture("session-catalog-local-unsubscribe.json")
    close_new["params"]["subscription_id"] = new_active_id  # type: ignore[index]
    controller.dispatch(close_new, transport)
    assert new_registration.close_calls == 1

    controller.dispatch(_fixture("session-catalog-local-subscribe.json"), transport)
    reused_registration = host.listener_registrations[-1]
    close_reused = _fixture("session-catalog-local-unsubscribe.json")
    close_reused["params"]["subscription_id"] = closed_ids[0]  # type: ignore[index]
    controller.dispatch(close_reused, transport)
    assert reused_registration.close_calls == 1
    assert transport.frames[-1]["result"]["closed"] is True  # type: ignore[index]

    controller.close_transport(transport)
    controller.dispatch(repeated, transport)
    assert transport.frames[-1]["error"]["reason"] == "transport_replaced"  # type: ignore[index]
