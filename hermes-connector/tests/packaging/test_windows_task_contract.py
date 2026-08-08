from __future__ import annotations

import sys
from pathlib import Path

import pytest

WINDOWS_PACKAGING = Path(__file__).parents[2] / "packaging" / "windows"
sys.path.insert(0, str(WINDOWS_PACKAGING))

from hermes_windows_tasks import (
    WindowsTaskContractError,
    build_connector_task,
    create_task_command,
    delete_task_command,
    end_task_command,
    query_task_command,
    render_connector_launcher,
    run_task_command,
)


def _task(tmp_path: Path):
    home = (tmp_path / "Hermes Home").resolve()
    release = (home / "releases" / "2026.08.08+1").resolve()
    config = (home / "connector" / "profiles" / "default" / "config.json").resolve()
    return build_connector_task(
        release_dir=release,
        release_id="2026.08.08+1",
        profile="default",
        hermes_home=home,
        config_file=config,
    )


def test_task_contract_points_only_to_exact_release(tmp_path: Path) -> None:
    task = _task(tmp_path)
    launcher = render_connector_launcher(task).decode("utf-8")

    assert task.task_name == "Hermes Connector [default]"
    assert task.executable == task.release_dir / "connector" / "hermes-connector.exe"
    assert task.launcher == task.release_dir / "services" / "windows" / "run-connector.cmd"
    assert task.release_id in launcher
    assert "hermes-connector.exe" in launcher
    assert task.release_dir.name in task.task_action
    assert "run-connector.cmd" in task.task_action


def test_launcher_contains_no_plaintext_credentials(tmp_path: Path) -> None:
    launcher = render_connector_launcher(_task(tmp_path)).decode("utf-8").lower()

    for marker in ("token", "password", "secret", "authorization", "credential"):
        assert marker not in launcher
    assert "hermes_home" in launcher
    assert "hermes_connector_config_file" in launcher


def test_scheduled_task_commands_are_per_user_and_non_elevated(tmp_path: Path) -> None:
    task = _task(tmp_path)
    create = create_task_command(task)

    assert create[:3] == ("schtasks.exe", "/Create", "/F")
    assert create[create.index("/SC") + 1] == "ONLOGON"
    assert create[create.index("/RL") + 1] == "LIMITED"
    assert "/RU" not in create
    assert "/RP" not in create
    assert run_task_command(task) == ("schtasks.exe", "/Run", "/TN", task.task_name)
    assert end_task_command(task) == ("schtasks.exe", "/End", "/TN", task.task_name)
    assert delete_task_command(task) == (
        "schtasks.exe",
        "/Delete",
        "/F",
        "/TN",
        task.task_name,
    )
    assert query_task_command(task)[0:2] == ("schtasks.exe", "/Query")


def test_task_rejects_release_identity_drift(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    with pytest.raises(WindowsTaskContractError, match="release identity"):
        build_connector_task(
            release_dir=(home / "releases" / "other").resolve(),
            release_id="2026.08.08+1",
            profile="default",
            hermes_home=home,
            config_file=(home / "config.json").resolve(),
        )


def test_task_rejects_config_outside_hermes_home(tmp_path: Path) -> None:
    home = (tmp_path / "home").resolve()
    release = (home / "releases" / "2026.08.08+1").resolve()
    with pytest.raises(WindowsTaskContractError, match="inside HERMES_HOME"):
        build_connector_task(
            release_dir=release,
            release_id="2026.08.08+1",
            profile="default",
            hermes_home=home,
            config_file=(tmp_path / "outside.json").resolve(),
        )
