"""Private Local Gateway registry trust behavior on macOS."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from local_trust_support import _private_directory, _registry_file


def test_read_private_registry_accepts_bounded_mode_0600_json(
    tmp_path: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        read_private_registry,
    )

    directory = _private_directory(tmp_path)
    payload = {"version": 1, "profile": "default"}
    path = _registry_file(directory, payload)

    assert read_private_registry(path, directory=directory) == payload


def test_read_private_registry_rejects_oversized_body_before_parsing(
    tmp_path: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        MAX_REGISTRY_BYTES,
        read_private_registry,
    )

    directory = _private_directory(tmp_path)
    path = directory / "gateway-1.json"
    path.write_bytes(b"{" + (b"x" * MAX_REGISTRY_BYTES))
    path.chmod(0o600)

    assert read_private_registry(path, directory=directory) is None


def test_read_private_registry_rejects_invalid_utf8(tmp_path: Path) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        read_private_registry,
    )

    directory = _private_directory(tmp_path)
    path = directory / "gateway-1.json"
    path.write_bytes(b'{"profile":"\xff"}')
    path.chmod(0o600)

    assert read_private_registry(path, directory=directory) is None


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660])
def test_read_private_registry_rejects_non_private_mode(
    tmp_path: Path,
    mode: int,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        read_private_registry,
    )

    directory = _private_directory(tmp_path)
    path = _registry_file(directory, {"version": 1}, mode=mode)

    assert read_private_registry(path, directory=directory) is None


def test_read_private_registry_rejects_symlink(tmp_path: Path) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        read_private_registry,
    )

    directory = _private_directory(tmp_path)
    target = tmp_path / "outside.json"
    target.write_text('{"version":1}', encoding="utf-8")
    target.chmod(0o600)
    path = directory / "gateway-1.json"
    path.symlink_to(target)

    assert read_private_registry(path, directory=directory) is None


def test_read_private_registry_rejects_fifo_without_opening_it(
    tmp_path: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        read_private_registry,
    )

    directory = _private_directory(tmp_path)
    path = directory / "gateway-1.json"
    os.mkfifo(path, mode=0o600)

    assert read_private_registry(path, directory=directory) is None


def test_read_private_registry_rejects_file_outside_dedicated_directory(
    tmp_path: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        read_private_registry,
    )

    directory = _private_directory(tmp_path)
    path = tmp_path / "outside.json"
    path.write_text('{"version":1}', encoding="utf-8")
    path.chmod(0o600)

    assert read_private_registry(path, directory=directory) is None


def test_registry_parse_failure_does_not_log_body(
    tmp_path: Path,
    caplog,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        read_private_registry,
    )

    directory = _private_directory(tmp_path)
    path = directory / "gateway-1.json"
    path.write_text('{"secret":"must-not-leak",', encoding="utf-8")
    path.chmod(0o600)

    assert read_private_registry(path, directory=directory) is None
    assert "must-not-leak" not in caplog.text
