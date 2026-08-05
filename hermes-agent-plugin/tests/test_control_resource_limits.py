from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from importlib.resources import files

import pytest


def test_relay_overloaded_uses_reserved_frozen_error_code() -> None:
    from hermes_agent_plugin.adapters.local_protocol.control_v1 import (
        CONTROL_ERROR_CODES,
        CONTROL_V1_ERROR_RANGE,
    )

    assert CONTROL_ERROR_CODES["relay_overloaded"] == 4215
    assert CONTROL_ERROR_CODES["relay_overloaded"] in CONTROL_V1_ERROR_RANGE


def test_relay_overloaded_is_present_in_packaged_contract_copy() -> None:
    contract_path = files("hermes_agent_plugin.contracts.generated").joinpath(
        "mobile-control-v1.json"
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))

    assert contract["error_codes"]["relay_overloaded"] == 4215


def test_bounded_owner_executor_rejects_overload_and_recovers_after_success() -> None:
    from hermes_agent_plugin.adapters.platform.macos.control_relay import (
        _BoundedExecutor,
    )

    started = threading.Event()
    release = threading.Event()

    def block() -> str:
        started.set()
        assert release.wait(timeout=2)
        return "first"

    executor = _BoundedExecutor(
        max_workers=1,
        max_queued=1,
        thread_name_prefix="bounded-owner-test",
    )
    try:
        first = executor.submit(block)
        assert first is not None
        assert started.wait(timeout=1)
        queued = executor.submit(lambda: "queued")
        assert queued is not None

        assert executor.submit(lambda: "overloaded") is None

        release.set()
        assert first.result(timeout=1) == "first"
        assert queued.result(timeout=1) == "queued"
        recovered = executor.submit(lambda: "recovered")
        assert recovered is not None
        assert recovered.result(timeout=1) == "recovered"
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_bounded_owner_executor_releases_permit_after_task_exception() -> None:
    from hermes_agent_plugin.adapters.platform.macos.control_relay import (
        _BoundedExecutor,
    )

    def fail() -> None:
        raise RuntimeError("owner failed")

    executor = _BoundedExecutor(
        max_workers=1,
        max_queued=0,
        thread_name_prefix="bounded-owner-exception-test",
    )
    try:
        failed = executor.submit(fail)
        assert failed is not None
        with pytest.raises(RuntimeError, match="owner failed"):
            failed.result(timeout=1)

        recovered = executor.submit(lambda: "recovered")
        assert recovered is not None
        assert recovered.result(timeout=1) == "recovered"
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_bounded_owner_executor_releases_permit_after_cancellation() -> None:
    from hermes_agent_plugin.adapters.platform.macos.control_relay import (
        _BoundedExecutor,
    )

    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        assert release.wait(timeout=2)

    executor = _BoundedExecutor(
        max_workers=1,
        max_queued=1,
        thread_name_prefix="bounded-owner-cancel-test",
    )
    try:
        running = executor.submit(block)
        assert running is not None
        assert started.wait(timeout=1)
        cancelled = executor.submit(lambda: None)
        assert cancelled is not None
        assert executor.submit(lambda: None) is None

        assert cancelled.cancel() is True
        recovered = executor.submit(lambda: "recovered")
        assert recovered is not None

        release.set()
        running.result(timeout=1)
        assert recovered.result(timeout=1) == "recovered"
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


class _IterableWebSocket:
    def __init__(self, incoming: list[str]) -> None:
        self.incoming = incoming
        self.sent: list[dict] = []
        self.closed = False
        self._send_lock = threading.Lock()

    def __iter__(self):
        return iter(self.incoming)

    def send(self, value: str) -> None:
        with self._send_lock:
            self.sent.append(json.loads(value))

    def close(self) -> None:
        self.closed = True


def _attach_request() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "attach",
        "method": "relay.control.attach",
        "params": {
            "claims": {
                "user_id": "user-1",
                "provider": "basic",
                "connection_role": "control",
                "client_instance_id": "11111111-1111-4111-8111-111111111111",
                "session_key": "session-1",
                "profile": "default",
            }
        },
    }


def _status_request(request_id: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session.control.status",
        "params": {
            "session_key": "session-1",
            "payload": "must-not-appear",
        },
    }


def test_control_owner_dispatch_returns_body_free_error_when_capacity_is_full() -> None:
    from hermes_agent_plugin.adapters.local_protocol.control_v1 import (
        CONTROL_ERROR_CODES,
    )
    from hermes_agent_plugin.adapters.platform.macos import control_relay
    from hermes_agent_plugin.adapters.platform.macos.control_relay import (
        _BoundedExecutor,
    )

    owner_action_dispatcher = _BoundedExecutor(
        max_workers=1,
        max_queued=1,
        thread_name_prefix="bounded-owner-connection-test",
    )
    release = threading.Event()
    websocket = _IterableWebSocket(
        [
            json.dumps(_attach_request()),
            json.dumps(_status_request("running")),
            json.dumps(_status_request("queued")),
            json.dumps(_status_request("overloaded")),
        ]
    )

    def block_dispatch(request, transport):
        assert release.wait(timeout=2)

    try:
        control_relay._handle_control_connection(
            websocket,
            dispatcher=block_dispatch,
            owner_action_dispatcher=owner_action_dispatcher,
        )

        overloaded = next(
            frame for frame in websocket.sent if frame.get("id") == "overloaded"
        )
        assert overloaded["error"] == {
            "code": CONTROL_ERROR_CODES["relay_overloaded"],
            "message": "relay_overloaded",
        }
        assert "must-not-appear" not in json.dumps(overloaded)
    finally:
        release.set()
        owner_action_dispatcher.shutdown(wait=True, cancel_futures=True)


def test_control_owner_capacity_is_shared_across_connections() -> None:
    from hermes_agent_plugin.adapters.local_protocol.control_v1 import (
        CONTROL_ERROR_CODES,
    )
    from hermes_agent_plugin.adapters.platform.macos import control_relay
    from hermes_agent_plugin.adapters.platform.macos.control_relay import (
        _BoundedExecutor,
    )

    owner_action_dispatcher = _BoundedExecutor(
        max_workers=1,
        max_queued=1,
        thread_name_prefix="bounded-owner-global-test",
    )
    release = threading.Event()
    first_connection = _IterableWebSocket(
        [
            json.dumps(_attach_request()),
            json.dumps(_status_request("running")),
            json.dumps(_status_request("queued")),
        ]
    )
    second_connection = _IterableWebSocket(
        [
            json.dumps(_attach_request()),
            json.dumps(_status_request("global-overload")),
        ]
    )

    def block_dispatch(request, transport):
        assert release.wait(timeout=2)

    try:
        control_relay._handle_control_connection(
            first_connection,
            dispatcher=block_dispatch,
            owner_action_dispatcher=owner_action_dispatcher,
        )
        control_relay._handle_control_connection(
            second_connection,
            dispatcher=block_dispatch,
            owner_action_dispatcher=owner_action_dispatcher,
        )

        overloaded = next(
            frame
            for frame in second_connection.sent
            if frame.get("id") == "global-overload"
        )
        assert overloaded["error"] == {
            "code": CONTROL_ERROR_CODES["relay_overloaded"],
            "message": "relay_overloaded",
        }
    finally:
        release.set()
        owner_action_dispatcher.shutdown(wait=True, cancel_futures=True)


def test_blocked_owner_action_does_not_hold_python_process_exit() -> None:
    inspection = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import threading;"
                "from hermes_agent_plugin.adapters.host.owner_actions "
                "import BoundedOwnerActionDispatcher;"
                "started=threading.Event();"
                "block=threading.Event();"
                "dispatcher=BoundedOwnerActionDispatcher("
                "max_workers=1,max_queued=0,"
                "thread_name_prefix='blocked-owner-exit');"
                "dispatcher.submit(lambda:(started.set(),block.wait()));"
                "assert started.wait(1);"
                "dispatcher.shutdown(wait=False,cancel_futures=True)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert inspection.returncode == 0, inspection.stderr


def test_owner_dispatcher_constructor_unwinds_workers_when_thread_start_fails(
    monkeypatch,
) -> None:
    from hermes_agent_plugin.adapters.host import owner_actions

    original_start = threading.Thread.start
    starts = 0

    def fail_second_start(thread) -> None:
        nonlocal starts
        starts += 1
        if starts == 2:
            raise RuntimeError("injected worker start failure")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_second_start)
    baseline = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("partial-owner-start-")
    }

    with pytest.raises(RuntimeError, match="injected worker start failure"):
        owner_actions.BoundedOwnerActionDispatcher(
            max_workers=3,
            max_queued=0,
            thread_name_prefix="partial-owner-start",
        )

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        current = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("partial-owner-start-")
        }
        if current == baseline:
            break
        time.sleep(0.01)
    assert current == baseline


class _PendingWebSocket:
    def __init__(self) -> None:
        self.incoming: queue.Queue[str | BaseException] = queue.Queue()
        self.incoming.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "relay-control-attach",
                    "result": {
                        "attached": True,
                        "connection_role": "control",
                    },
                }
            )
        )
        self.sent: list[dict] = []
        self.closed = False
        self.fail_request_send = False
        self._sent_condition = threading.Condition()

    def send(self, value: str) -> None:
        frame = json.loads(value)
        if frame.get("method") != "relay.control.attach" and self.fail_request_send:
            raise RuntimeError("upstream send failed")
        with self._sent_condition:
            self.sent.append(frame)
            self._sent_condition.notify_all()

    def recv(self, timeout=None):
        item = self.incoming.get(timeout=timeout)
        if isinstance(item, BaseException):
            raise item
        return item

    def wait_for_sent(self, count: int) -> bool:
        with self._sent_condition:
            return self._sent_condition.wait_for(
                lambda: len(self.sent) >= count,
                timeout=1,
            )

    def respond_to(self, internal_id: str, result: dict) -> None:
        self.incoming.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": internal_id,
                    "result": result,
                }
            )
        )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.incoming.put(RuntimeError("closed"))


class _Downstream:
    def write(self, frame: dict) -> bool:
        return True


def _relay_connection(websocket: _PendingWebSocket, *, max_pending_rpcs: int):
    from hermes_agent_plugin.adapters.platform.macos.control_relay import (
        _RelayConnection,
    )

    return _RelayConnection(
        websocket=websocket,
        downstream=_Downstream(),
        claims={},
        endpoint_instance_id="owner",
        on_finished=lambda connection, unexpected: None,
        max_pending_rpcs=max_pending_rpcs,
    )


def _run_call(connection, request: dict):
    result: list[dict | None] = []
    finished = threading.Event()

    def call() -> None:
        result.append(connection.call(request))
        finished.set()

    thread = threading.Thread(target=call)
    thread.start()
    return thread, finished, result


def test_relay_pending_limit_rejects_without_sending_upstream() -> None:
    from hermes_agent_plugin.adapters.local_protocol.control_v1 import (
        CONTROL_ERROR_CODES,
    )

    websocket = _PendingWebSocket()
    connection = _relay_connection(websocket, max_pending_rpcs=1)
    first_thread = None
    second_thread = None
    try:
        first_thread, _, _ = _run_call(
            connection,
            _status_request("first"),
        )
        assert websocket.wait_for_sent(2)
        sent_before_overload = len(websocket.sent)

        second_thread, second_finished, second_result = _run_call(
            connection,
            _status_request("second"),
        )

        assert second_finished.wait(timeout=0.2)
        assert second_result == [
            {
                "jsonrpc": "2.0",
                "id": "second",
                "error": {
                    "code": CONTROL_ERROR_CODES["relay_overloaded"],
                    "message": "relay_overloaded",
                },
            }
        ]
        assert len(websocket.sent) == sent_before_overload
        assert "must-not-appear" not in json.dumps(second_result)
    finally:
        connection.close()
        if first_thread is not None:
            first_thread.join(timeout=1)
        if second_thread is not None:
            second_thread.join(timeout=1)


def test_relay_pending_slot_recovers_after_successful_response() -> None:
    websocket = _PendingWebSocket()
    connection = _relay_connection(websocket, max_pending_rpcs=1)
    first_thread = None
    second_thread = None
    try:
        first_thread, first_finished, first_result = _run_call(
            connection,
            _status_request("first"),
        )
        assert websocket.wait_for_sent(2)
        websocket.respond_to(websocket.sent[-1]["id"], {"status": "first"})
        assert first_finished.wait(timeout=1)
        assert first_result[0]["id"] == "first"

        second_thread, second_finished, second_result = _run_call(
            connection,
            _status_request("second"),
        )
        assert websocket.wait_for_sent(3)
        websocket.respond_to(websocket.sent[-1]["id"], {"status": "second"})
        assert second_finished.wait(timeout=1)
        assert second_result[0]["id"] == "second"
    finally:
        connection.close()
        if first_thread is not None:
            first_thread.join(timeout=1)
        if second_thread is not None:
            second_thread.join(timeout=1)


def test_relay_pending_slot_recovers_after_send_exception() -> None:
    websocket = _PendingWebSocket()
    connection = _relay_connection(websocket, max_pending_rpcs=1)
    try:
        websocket.fail_request_send = True

        assert connection.call(_status_request("failed")) is None
        assert connection._pending == {}

        websocket.fail_request_send = False
        thread, finished, _ = _run_call(
            connection,
            _status_request("recovered"),
        )
        assert websocket.wait_for_sent(2)
        connection.close()
        assert finished.wait(timeout=1)
        thread.join(timeout=1)
    finally:
        connection.close()


def test_relay_close_clears_orphaned_pending_entries() -> None:
    websocket = _PendingWebSocket()
    connection = _relay_connection(websocket, max_pending_rpcs=1)
    orphan: queue.Queue[dict | None] = queue.Queue(maxsize=1)
    with connection._pending_lock:
        connection._pending["orphan"] = orphan

    connection.close()

    assert connection._pending == {}
    assert orphan.get_nowait() is None
