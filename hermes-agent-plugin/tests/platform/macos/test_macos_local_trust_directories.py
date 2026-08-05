"""Private-directory and profile trust behavior on macOS."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "profile",
    [
        "a",
        "Default_01.alpha-beta",
        "x" * 128,
    ],
)
def test_validate_profile_accepts_only_canonical_values(profile: str) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import validate_profile

    assert validate_profile(profile) == profile


@pytest.mark.parametrize(
    "profile",
    [
        "",
        "x" * 129,
        "has space",
        "has/slash",
        "é",
        None,
        1,
    ],
)
def test_validate_profile_fails_closed_for_invalid_values(profile: object) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import validate_profile

    with pytest.raises(ValueError, match="invalid local profile"):
        validate_profile(profile)


def test_ensure_private_directory_creates_mode_0700_directory(
    tmp_path: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        ensure_private_directory,
        is_private_directory,
    )

    directory = tmp_path / "runtime"

    assert ensure_private_directory(directory) == directory
    assert is_private_directory(directory) is True
    assert stat.S_IMODE(directory.lstat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o750, 0o701, 0o777])
def test_private_directory_rejects_group_or_other_permissions(
    tmp_path: Path,
    mode: int,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        ensure_private_directory,
        is_private_directory,
    )

    directory = tmp_path / "runtime"
    directory.mkdir()
    directory.chmod(mode)

    assert is_private_directory(directory) is False
    with pytest.raises(ValueError, match="untrusted local directory"):
        ensure_private_directory(directory)


def test_private_directory_rejects_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_trust import (
        ensure_private_directory,
        is_private_directory,
    )

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    directory = tmp_path / "runtime"
    directory.symlink_to(target, target_is_directory=True)

    assert is_private_directory(directory) is False
    with pytest.raises(ValueError, match="untrusted local directory"):
        ensure_private_directory(directory)


def test_private_directory_rejects_path_owned_by_another_uid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos import local_trust

    directory = tmp_path / "runtime"
    directory.mkdir(mode=0o700)
    current_uid = os.getuid()
    monkeypatch.setattr(local_trust.os, "getuid", lambda: current_uid + 1)

    assert local_trust.is_private_directory(directory) is False
