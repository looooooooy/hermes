from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

MACOS_PACKAGING = Path(__file__).parents[2] / "packaging" / "macos"
sys.path.insert(0, str(MACOS_PACKAGING))

from hermes_macos_launch_agents import (
    LaunchAgentError,
    render_release_launch_agents,
    write_release_launch_agents,
)


def test_launch_agents_pin_exact_release_executables_and_safe_restart_policy(
    tmp_path: Path,
) -> None:
    release_dir = tmp_path / "releases" / "2026.08.03-b1"

    rendered = render_release_launch_agents(release_dir)

    host = plistlib.loads(rendered["host"])
    connector = plistlib.loads(rendered["connector"])
    assert host["ProgramArguments"] == [
        str(release_dir / "host" / "venv" / "bin" / "hermes")
    ]
    assert connector["ProgramArguments"] == [
        str(release_dir / "connector" / "venv" / "bin" / "hermes-connector"),
        "run",
        "--release-id",
        "2026.08.03-b1",
    ]
    assert host["EnvironmentVariables"] == {
        "HERMES_PLUGIN_STORE_MANIFEST": str(
            release_dir / "plugin" / "metadata" / "signed-plugin-manifest.json"
        ),
        "HERMES_PLUGIN_STORE_TRUST_STORE": str(
            release_dir / "plugin" / "metadata" / "trust-store.json"
        ),
    }
    assert "EnvironmentVariables" not in connector
    for payload in (host, connector):
        assert payload["KeepAlive"] == {"SuccessfulExit": False}
        assert payload["RunAtLoad"] is True
        assert payload["ThrottleInterval"] >= 10
        assert payload["Umask"] == 0o077
        assert payload["StandardOutPath"].startswith(
            str(release_dir / "receipts" / "logs")
        )
        assert payload["StandardErrorPath"].startswith(
            str(release_dir / "receipts" / "logs")
        )
        assert all(
            "/usr/bin/env" not in argument for argument in payload["ProgramArguments"]
        )


def test_writer_creates_private_plists_without_invoking_launchctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_dir = tmp_path / "releases" / "2026.08.03-b1"
    (release_dir / "services").mkdir(parents=True)
    calls: list[object] = []
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    paths = write_release_launch_agents(release_dir)

    assert calls == []
    assert set(paths) == {"host", "connector"}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths.values())


@pytest.mark.parametrize(
    "release_dir", [Path("relative/release"), Path("/tmp/releases/current")]
)
def test_launch_agent_requires_absolute_versioned_release_path(
    release_dir: Path,
) -> None:
    with pytest.raises(LaunchAgentError, match="versioned absolute"):
        render_release_launch_agents(release_dir)
