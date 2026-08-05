"""Private Unix socket trust and cleanup behavior on macOS."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from local_trust_support import (
    _bind_unix_socket,
    _private_directory,
    _registry_file,
)


def test_private_socket_accepts_owned_mode_0600_unix_socket(
    short_private_directory: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        is_private_socket,
    )

    path = short_private_directory / "owner.sock"
    server = _bind_unix_socket(path)
    try:
        assert is_private_socket(path, directory=short_private_directory) is True
    finally:
        server.close()


@pytest.mark.parametrize("kind", ["regular", "fifo"])
def test_private_socket_rejects_non_socket_file(
    tmp_path: Path,
    kind: str,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        is_private_socket,
    )

    directory = _private_directory(tmp_path)
    path = directory / "owner.sock"
    if kind == "regular":
        path.touch(mode=0o600)
    else:
        os.mkfifo(path, mode=0o600)

    assert is_private_socket(path, directory=directory) is False


def test_private_socket_rejects_symlink_to_real_socket(
    short_private_directory: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        is_private_socket,
    )

    target = short_private_directory / "target.sock"
    server = _bind_unix_socket(target)
    path = short_private_directory / "owner.sock"
    path.symlink_to(target)
    try:
        assert is_private_socket(path, directory=short_private_directory) is False
    finally:
        server.close()


def test_private_socket_rejects_group_access(
    short_private_directory: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        is_private_socket,
    )

    path = short_private_directory / "owner.sock"
    server = _bind_unix_socket(path)
    path.chmod(0o660)
    try:
        assert is_private_socket(path, directory=short_private_directory) is False
    finally:
        server.close()


def test_private_socket_rejects_path_outside_dedicated_directory(
    short_private_directory: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        is_private_socket,
    )

    other_directory = short_private_directory / "other"
    other_directory.mkdir(mode=0o700)
    path = other_directory / "owner.sock"
    server = _bind_unix_socket(path)
    try:
        assert is_private_socket(path, directory=short_private_directory) is False
    finally:
        server.close()


def test_private_socket_rejects_untrusted_dedicated_directory(
    short_private_directory: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        is_private_socket,
    )

    path = short_private_directory / "owner.sock"
    server = _bind_unix_socket(path)
    short_private_directory.chmod(0o755)
    try:
        assert is_private_socket(path, directory=short_private_directory) is False
    finally:
        short_private_directory.chmod(0o700)
        server.close()


def test_unlink_private_registry_never_follows_symlink(
    tmp_path: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        unlink_private_registry,
    )

    directory = _private_directory(tmp_path)
    target = tmp_path / "victim.json"
    target.write_text("do not delete", encoding="utf-8")
    target.chmod(0o600)
    path = directory / "gateway-1.json"
    path.symlink_to(target)

    assert unlink_private_registry(path, directory=directory) is False
    assert target.read_text(encoding="utf-8") == "do not delete"
    assert path.is_symlink()


def test_unlink_private_registry_removes_validated_regular_file(
    tmp_path: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        unlink_private_registry,
    )

    directory = _private_directory(tmp_path)
    path = _registry_file(directory, {"version": 1})

    assert unlink_private_registry(path, directory=directory) is True
    assert path.exists() is False


def test_unlink_private_socket_rejects_regular_file(tmp_path: Path) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        unlink_private_socket,
    )

    directory = _private_directory(tmp_path)
    path = directory / "owner.sock"
    path.touch(mode=0o600)

    assert unlink_private_socket(path, directory=directory) is False
    assert path.exists() is True


def test_unlink_private_socket_removes_validated_socket(
    short_private_directory: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        unlink_private_socket,
    )

    path = short_private_directory / "owner.sock"
    server = _bind_unix_socket(path)
    try:
        assert unlink_private_socket(path, directory=short_private_directory) is True
        assert path.exists() is False
    finally:
        server.close()
