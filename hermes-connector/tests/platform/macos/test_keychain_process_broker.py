from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path

import pytest

from hermes_connector.adapters.platform.macos.keychain import (
    KeychainSecretUnavailable,
    MacOSKeychainSecretStore,
)
from hermes_connector.adapters.platform.macos.keychain_broker import (
    BrokerRequest,
    KeychainBrokerEffectUnknown,
    MacOSKeychainBroker,
    _terminate_process,
    decode_broker_request,
    decode_broker_response,
    encode_broker_request,
    encode_broker_response,
)

FIXTURES = Path(__file__).parents[2] / "fixtures"
FAKE_HELPER_COMMAND = (
    sys.executable,
    str(FIXTURES / "fake_keychain_helper.py"),
)


def _store(
    tmp_path: Path,
    *,
    timeout_seconds: float = 2.0,
) -> tuple[MacOSKeychainSecretStore, MacOSKeychainBroker, Path, Path]:
    state_path = tmp_path / "fake-keychain"
    mode_path = state_path.with_suffix(".mode")
    broker = MacOSKeychainBroker(
        operation_timeout_seconds=timeout_seconds,
        helper_command=FAKE_HELPER_COMMAND,
    )
    store = MacOSKeychainSecretStore(
        service=str(state_path),
        account="test-account",
        broker=broker,
    )
    return store, broker, state_path, mode_path


def _assert_reaped(state_path: Path) -> None:
    for pid in state_path.with_suffix(".pids").read_text(encoding="ascii").splitlines():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


class _ExitRacingProcess:
    def __init__(self, race_stage: str) -> None:
        self.returncode = None
        self._race_stage = race_stage
        self._wait_calls = 0

    async def wait(self) -> int:
        self._wait_calls += 1
        if self._race_stage == "kill" and self._wait_calls == 1:
            await asyncio.Future()
        self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        if self._race_stage == "terminate":
            self.returncode = 0
            raise ProcessLookupError

    def kill(self) -> None:
        self.returncode = 0
        raise ProcessLookupError


@pytest.mark.parametrize("mutation", ("truncated", "extra", "version", "operation"))
def test_broker_request_codec_rejects_noncanonical_frames(mutation: str) -> None:
    request = BrokerRequest(
        operation="write",
        request_id=b"I" * 16,
        service=b"service",
        account=b"account",
        payload=b"secret",
    )
    valid = encode_broker_request(request)
    if mutation == "truncated":
        invalid = valid[:-1]
    elif mutation == "extra":
        invalid = valid + b"x"
    elif mutation == "version":
        invalid = bytes((2,)) + valid[1:]
    else:
        invalid = valid[:1] + b"\xff" + valid[2:]

    with pytest.raises(ValueError):
        decode_broker_request(invalid)


@pytest.mark.parametrize("mutation", ("truncated", "extra", "version", "request_id"))
def test_broker_response_codec_rejects_noncanonical_frames(mutation: str) -> None:
    request_id = b"I" * 16
    valid = encode_broker_response(request_id, b"\x01secret")
    if mutation == "truncated":
        invalid = valid[:-1]
    elif mutation == "extra":
        invalid = valid + b"x"
    elif mutation == "version":
        invalid = bytes((2,)) + valid[1:]
    else:
        invalid = valid
        request_id = b"J" * 16

    with pytest.raises(ValueError):
        decode_broker_response(invalid, request_id=request_id)


@pytest.mark.asyncio
async def test_hung_helper_is_terminated_joined_and_fails_closed(
    tmp_path: Path,
) -> None:
    store, broker, state_path, mode_path = _store(tmp_path, timeout_seconds=1)
    mode_path.write_text("hang_before", encoding="ascii")

    started = time.monotonic()
    with pytest.raises(KeychainSecretUnavailable):
        await store.write_secret(b"stale-secret")

    assert time.monotonic() - started < 2.3
    assert not state_path.exists()
    _assert_reaped(state_path)
    await broker.aclose()


@pytest.mark.asyncio
async def test_executable_helper_uses_the_same_strict_pipe_protocol(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "fake-keychain"
    state_path.write_bytes(b"legacy-secret")
    broker = MacOSKeychainBroker(
        operation_timeout_seconds=2,
        helper_command=FAKE_HELPER_COMMAND,
    )
    store = MacOSKeychainSecretStore(
        service=str(state_path),
        account="test-account",
        broker=broker,
    )

    assert await store.read_secret() == b"legacy-secret"

    _assert_reaped(state_path)
    await broker.aclose()


@pytest.mark.asyncio
async def test_just_before_kill_write_is_confirmed_by_fresh_readback(
    tmp_path: Path,
) -> None:
    store, broker, state_path, mode_path = _store(tmp_path)
    mode_path.write_text("mutate_then_hang", encoding="ascii")

    await store.write_secret(b"committed-secret")

    assert await store.read_secret() == b"committed-secret"
    _assert_reaped(state_path)
    await broker.aclose()


@pytest.mark.asyncio
async def test_cancel_kills_helper_before_later_write_and_cannot_overwrite_it(
    tmp_path: Path,
) -> None:
    store, broker, state_path, mode_path = _store(tmp_path)
    mode_path.write_text("mutate_then_hang", encoding="ascii")
    stale = asyncio.create_task(store.write_secret(b"stale-secret"))
    while not state_path.exists():
        await asyncio.sleep(0)

    stale.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stale
    await store.write_secret(b"current-secret")
    await asyncio.sleep(0.05)

    assert await store.read_secret() == b"current-secret"
    _assert_reaped(state_path)
    await broker.aclose()


@pytest.mark.asyncio
async def test_uncertain_delete_rechecks_digest_and_does_not_resurrect(
    tmp_path: Path,
) -> None:
    store, broker, state_path, mode_path = _store(tmp_path)
    await store.write_secret(b"old-secret")
    mode_path.write_text("mutate_then_hang", encoding="ascii")

    assert await store.delete_secret()
    assert await store.create_secret(b"new-secret")
    await asyncio.sleep(0.05)

    assert await store.read_secret() == b"new-secret"
    _assert_reaped(state_path)
    await broker.aclose()


@pytest.mark.asyncio
async def test_recovery_timeout_reports_effect_unknown_and_reaps_both_helpers(
    tmp_path: Path,
) -> None:
    store, broker, state_path, mode_path = _store(tmp_path)
    mode_path.write_text("mutate_then_hang_recovery", encoding="ascii")

    with pytest.raises(KeychainBrokerEffectUnknown):
        await store.write_secret(b"uncertain-secret")

    _assert_reaped(state_path)
    await broker.aclose()


@pytest.mark.asyncio
async def test_sigterm_resistant_helper_is_killed_and_reaped(
    tmp_path: Path,
) -> None:
    store, broker, state_path, mode_path = _store(tmp_path)
    mode_path.write_text("ignore_sigterm", encoding="ascii")

    with pytest.raises(KeychainSecretUnavailable):
        await store.write_secret(b"never-written")

    _assert_reaped(state_path)
    await broker.aclose()


@pytest.mark.asyncio
async def test_double_cancel_cannot_release_lock_before_sigterm_resistant_reap(
    tmp_path: Path,
) -> None:
    for race_stage in ("terminate", "kill"):
        process = _ExitRacingProcess(race_stage)
        await _terminate_process(process, terminate=race_stage == "terminate")
        assert process.returncode == 0

    store, broker, state_path, mode_path = _store(tmp_path)
    mode_path.write_text("ignore_sigterm", encoding="ascii")
    stale = asyncio.create_task(store.write_secret(b"stale-secret"))
    ready_path = state_path.with_suffix(".ready")
    while not ready_path.exists():
        await asyncio.sleep(0)

    stale.cancel()
    await asyncio.sleep(0)
    stale.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stale

    _assert_reaped(state_path)
    await store.write_secret(b"current-secret")
    assert await store.read_secret() == b"current-secret"
    _assert_reaped(state_path)
    await broker.aclose()


@pytest.mark.asyncio
async def test_supervised_broker_stop_reaps_active_helper_and_ends_run_loop(
    tmp_path: Path,
) -> None:
    store, broker, state_path, mode_path = _store(tmp_path, timeout_seconds=1)
    mode_path.write_text("hang_before", encoding="ascii")
    await broker.start()
    run_task = asyncio.create_task(broker.run())
    mutation = asyncio.create_task(store.write_secret(b"never-written"))
    pid_path = state_path.with_suffix(".pids")
    while not pid_path.exists():
        await asyncio.sleep(0)

    await broker.stop()

    await run_task
    with pytest.raises(KeychainBrokerEffectUnknown):
        await mutation
    assert not await broker.ready()
    _assert_reaped(state_path)


@pytest.mark.asyncio
async def test_legacy_plain_value_reads_compatibly_and_next_write_envelopes_once(
    tmp_path: Path,
) -> None:
    store, broker, state_path, _ = _store(tmp_path)
    state_path.write_bytes(b"legacy-private-seed")

    assert await store.read_secret() == b"legacy-private-seed"
    assert state_path.read_bytes() == b"legacy-private-seed"

    await store.write_secret(b"legacy-private-seed")
    enveloped = state_path.read_bytes()
    assert enveloped != b"legacy-private-seed"
    assert await store.read_secret() == b"legacy-private-seed"

    await store.write_secret(b"legacy-private-seed")
    assert state_path.read_bytes() != enveloped
    assert await store.read_secret() == b"legacy-private-seed"
    await broker.aclose()


@pytest.mark.asyncio
async def test_same_payload_new_revision_survives_stale_compare_delete(
    tmp_path: Path,
) -> None:
    store, broker, state_path, mode_path = _store(tmp_path)
    await store.write_secret(b"same-secret")
    mode_path.write_text("replace_before_delete", encoding="ascii")

    deleted = await store.delete_secret_if_matches(
        hashlib.sha256(b"same-secret").digest()
    )

    assert not deleted
    assert await store.read_secret() == b"same-secret"
    assert state_path.exists()
    await broker.aclose()


@pytest.mark.asyncio
async def test_slow_helper_startup_does_not_block_event_loop_or_escape_deadline(
    tmp_path: Path,
) -> None:
    broker = MacOSKeychainBroker(
        operation_timeout_seconds=0.05,
        helper_command=(
            sys.executable,
            str(FIXTURES / "slow_keychain_helper.py"),
        ),
    )
    store = MacOSKeychainSecretStore(
        service=str(tmp_path / "fake-keychain"),
        account="test-account",
        broker=broker,
    )
    heartbeat = 0
    running = True

    async def pulse() -> None:
        nonlocal heartbeat
        while running:
            heartbeat += 1
            await asyncio.sleep(0.005)

    pulse_task = asyncio.create_task(pulse())
    started = time.monotonic()
    try:
        with pytest.raises(KeychainBrokerEffectUnknown):
            await store.write_secret(b"x" * 16_384)
    finally:
        running = False
        await pulse_task
        await broker.aclose()

    assert time.monotonic() - started < 0.3
    assert heartbeat >= 5


@pytest.mark.asyncio
async def test_helper_that_never_reads_stdin_is_killed_without_blocking_loop(
    tmp_path: Path,
) -> None:
    broker = MacOSKeychainBroker(
        operation_timeout_seconds=0.05,
        helper_command=(
            sys.executable,
            str(FIXTURES / "no_read_keychain_helper.py"),
        ),
    )
    store = MacOSKeychainSecretStore(
        service=str(tmp_path / "fake-keychain"),
        account="test-account",
        broker=broker,
    )
    heartbeat = 0
    running = True

    async def pulse() -> None:
        nonlocal heartbeat
        while running:
            heartbeat += 1
            await asyncio.sleep(0.005)

    pulse_task = asyncio.create_task(pulse())
    started = time.monotonic()
    try:
        with pytest.raises(KeychainBrokerEffectUnknown):
            await store.write_secret(b"x" * 16_384)
    finally:
        running = False
        await pulse_task
        await broker.aclose()

    assert time.monotonic() - started < 0.3
    assert heartbeat >= 5


@pytest.mark.asyncio
async def test_default_helper_cannot_be_shadowed_by_malicious_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "hermes_connector" / "adapters" / "platform" / "macos"
    package.mkdir(parents=True)
    for parent in (
        package.parents[2],
        package.parents[1],
        package.parents[0],
        package,
    ):
        (parent / "__init__.py").write_text("", encoding="utf-8")
    marker = tmp_path / "malicious-helper-read"
    (package / "keychain_helper.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_bytes(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    broker = MacOSKeychainBroker(operation_timeout_seconds=1)

    try:
        await broker.read_secret(b"trusted-service", b"secret-sentinel")
    except KeychainSecretUnavailable:
        pass
    finally:
        await broker.aclose()

    assert not marker.exists()


def test_production_broker_has_no_pickle_shell_or_secret_process_arguments() -> None:
    source = (
        Path(__file__).parents[3]
        / "src/hermes_connector/adapters/platform/macos/keychain_broker.py"
    ).read_text(encoding="utf-8")

    assert "pickle" not in source
    assert "/usr/bin/security" not in source
    assert "ThreadPoolExecutor" not in source
    assert "shell=True" not in source
    assert '"-I",' in source
    assert '"-m",' in source
    assert '"hermes_connector.adapters.platform.macos.keychain_helper"' in source
