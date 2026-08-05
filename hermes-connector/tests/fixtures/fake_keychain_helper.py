from __future__ import annotations

import hashlib
import os
import signal
import time
from pathlib import Path

from hermes_connector.adapters.platform.macos.keychain_broker import (
    _MAX_REQUEST_BYTES,
    _read_frame_blocking,
    _write_frame_blocking,
    decode_broker_request,
    encode_broker_response,
)


def main() -> None:
    request = decode_broker_request(
        _read_frame_blocking(0, maximum_bytes=_MAX_REQUEST_BYTES)
    )
    state_path = Path(request.service.decode("utf-8"))
    mode_path = state_path.with_suffix(".mode")
    pid_path = state_path.with_suffix(".pids")
    with pid_path.open("a", encoding="ascii") as stream:
        stream.write(f"{os.getpid()}\n")
    mode = mode_path.read_text(encoding="ascii") if mode_path.exists() else "normal"
    if mode == "ignore_sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        mode_path.write_text("normal", encoding="ascii")
        state_path.with_suffix(".ready").write_text("ready", encoding="ascii")
        while True:
            time.sleep(1)
    if mode == "hang_before":
        mode_path.write_text("normal", encoding="ascii")
        while True:
            time.sleep(1)

    current = state_path.read_bytes() if state_path.exists() else None
    if request.operation == "read":
        if mode == "hang_read":
            while True:
                time.sleep(1)
        response = b"\x00" if current is None else b"\x01" + current
    elif request.operation == "create":
        created = current is None
        if created:
            state_path.write_bytes(request.payload)
        response = b"\x01" if created else b"\x00"
    elif request.operation == "write":
        state_path.write_bytes(request.payload)
        response = b""
    elif request.operation == "delete_if_digest":
        if mode == "replace_before_delete" and current is not None:
            replacement = bytearray(current)
            replacement[9:25] = b"R" * 16
            state_path.write_bytes(replacement)
            current = bytes(replacement)
            mode_path.write_text("normal", encoding="ascii")
        deleted = (
            current is not None and hashlib.sha256(current).digest() == request.payload
        )
        if deleted:
            state_path.unlink()
        response = b"\x01" if deleted else b"\x00"
    else:
        raise AssertionError("unexpected operation")

    if mode == "mutate_then_hang" and request.operation != "read":
        mode_path.write_text("normal", encoding="ascii")
        while True:
            time.sleep(1)
    if mode == "mutate_then_hang_recovery" and request.operation != "read":
        mode_path.write_text("hang_read", encoding="ascii")
        while True:
            time.sleep(1)
    _write_frame_blocking(
        1,
        encode_broker_response(request.request_id, response),
    )


if __name__ == "__main__":
    main()
