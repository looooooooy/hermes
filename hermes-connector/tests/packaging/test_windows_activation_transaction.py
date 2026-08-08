from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

WINDOWS_PACKAGING = Path(__file__).parents[2] / "packaging" / "windows"
sys.path.insert(0, str(WINDOWS_PACKAGING))

from hermes_windows_activation import (
    WindowsActivationBlocked,
    WindowsActivationController,
    WindowsActivationError,
)
from hermes_windows_release import (
    WindowsReleaseValidationError,
    render_windows_runtime_evidence,
    validate_windows_release,
)


class MemoryStore:
    def __init__(self, *, launcher_path: Path) -> None:
        self.launcher_path = launcher_path
        self.files: dict[str, bytes] = {}
        self.launcher: bytes | None = None
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield

    def read(self, name: str) -> bytes | None:
        return self.files.get(name)

    def write(self, name: str, payload: bytes) -> None:
        self.files[name] = payload

    def delete(self, name: str) -> None:
        self.files.pop(name, None)

    def write_launcher(self, payload: bytes) -> None:
        self.launcher = payload

    def delete_launcher(self) -> None:
        self.launcher = None


class FakePlatform:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.health: dict[str, bool] = {}
        self.fail_register: set[str] = set()

    def end(self, task) -> None:
        self.events.append(("end", task.release_id))

    def register(self, task) -> None:
        self.events.append(("register", task.release_id))
        if task.release_id in self.fail_register:
            raise RuntimeError("register failed")

    def run(self, task) -> None:
        self.events.append(("run", task.release_id))

    def delete(self, task) -> None:
        self.events.append(("delete", task.release_id))

    def healthy(self, task, *, timeout_seconds: float) -> bool:
        assert timeout_seconds > 0
        self.events.append(("health", task.release_id))
        return self.health.get(task.release_id, False)


def _release(root: Path, release_id: str, marker: bytes) -> Path:
    release = (root / "releases" / release_id).resolve()
    (release / "manifest").mkdir(parents=True)
    (release / "connector").mkdir()
    (release / "receipts").mkdir()
    release_digest = (marker.hex() * 64)[:64]
    (release / "manifest" / "release.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": release_id,
                "release_digest": release_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    (release / "connector" / "hermes-connector.exe").write_bytes(
        b"frozen-connector:" + marker
    )
    evidence = render_windows_runtime_evidence(
        release_dir=release,
        expected_release_id=release_id,
    )
    (release / "receipts" / "windows-runtime.json").write_bytes(evidence)
    return release


def _controller(tmp_path: Path, platform: FakePlatform, store: MemoryStore):
    home = (tmp_path / "home").resolve()
    config = (home / "connector" / "profiles" / "default" / "config.json").resolve()
    return WindowsActivationController(
        profile="default",
        hermes_home=home,
        config_file=config,
        store=store,
        platform=platform,
        health_timeout_seconds=2.0,
    )


def _store(tmp_path: Path) -> MemoryStore:
    home = (tmp_path / "home").resolve()
    return MemoryStore(
        launcher_path=(
            home
            / "connector"
            / "profiles"
            / "default"
            / "activation"
            / "run-connector.cmd"
        )
    )


def _active(store: MemoryStore) -> dict[str, object]:
    return json.loads(store.files["active.json"].decode("utf-8"))


def test_release_evidence_is_bound_to_common_release_and_executable(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path, "2026.08.08+1", b"a")

    validated = validate_windows_release(
        release_dir=release,
        expected_release_id="2026.08.08+1",
    )

    assert validated.release_id == "2026.08.08+1"
    assert validated.release_dir == release
    (release / "connector" / "hermes-connector.exe").write_bytes(b"tampered")
    with pytest.raises(WindowsReleaseValidationError, match="digest does not match"):
        validate_windows_release(
            release_dir=release,
            expected_release_id="2026.08.08+1",
        )


def test_first_activation_writes_active_only_after_health(tmp_path: Path) -> None:
    release = _release(tmp_path, "2026.08.08+1", b"a")
    store = _store(tmp_path)
    platform = FakePlatform()
    platform.health["2026.08.08+1"] = True
    controller = _controller(tmp_path, platform, store)

    result = controller.activate(release_dir=release, release_id="2026.08.08+1")

    assert result.changed is True
    assert result.previous is None
    assert "pending.json" not in store.files
    assert "blocked.json" not in store.files
    assert _active(store)["active"]["release_id"] == "2026.08.08+1"
    assert b"2026.08.08+1" in (store.launcher or b"")
    assert platform.events == [
        ("register", "2026.08.08+1"),
        ("run", "2026.08.08+1"),
        ("health", "2026.08.08+1"),
    ]


def test_n_to_n_plus_one_preserves_previous_exact_release(tmp_path: Path) -> None:
    first = _release(tmp_path, "2026.08.08+1", b"a")
    second = _release(tmp_path, "2026.08.08+2", b"b")
    store = _store(tmp_path)
    platform = FakePlatform()
    platform.health.update({"2026.08.08+1": True, "2026.08.08+2": True})
    controller = _controller(tmp_path, platform, store)
    controller.activate(release_dir=first, release_id="2026.08.08+1")
    platform.events.clear()

    result = controller.activate(release_dir=second, release_id="2026.08.08+2")

    assert result.active.release_id == "2026.08.08+2"
    assert result.previous is not None
    assert result.previous.release_id == "2026.08.08+1"
    active = _active(store)
    assert active["active"]["release_id"] == "2026.08.08+2"
    assert active["previous"]["release_id"] == "2026.08.08+1"
    assert platform.events == [
        ("end", "2026.08.08+2"),
        ("register", "2026.08.08+2"),
        ("run", "2026.08.08+2"),
        ("health", "2026.08.08+2"),
    ]


def test_same_healthy_release_is_idempotent(tmp_path: Path) -> None:
    release = _release(tmp_path, "2026.08.08+1", b"a")
    store = _store(tmp_path)
    platform = FakePlatform()
    platform.health["2026.08.08+1"] = True
    controller = _controller(tmp_path, platform, store)
    controller.activate(release_dir=release, release_id="2026.08.08+1")
    platform.events.clear()

    result = controller.activate(release_dir=release, release_id="2026.08.08+1")

    assert result.changed is False
    assert platform.events == [("health", "2026.08.08+1")]


def test_candidate_health_failure_rolls_back_previous_release(tmp_path: Path) -> None:
    first = _release(tmp_path, "2026.08.08+1", b"a")
    second = _release(tmp_path, "2026.08.08+2", b"b")
    store = _store(tmp_path)
    platform = FakePlatform()
    platform.health["2026.08.08+1"] = True
    controller = _controller(tmp_path, platform, store)
    controller.activate(release_dir=first, release_id="2026.08.08+1")
    platform.health["2026.08.08+2"] = False
    platform.events.clear()

    with pytest.raises(WindowsActivationError, match="activation failed"):
        controller.activate(release_dir=second, release_id="2026.08.08+2")

    assert _active(store)["active"]["release_id"] == "2026.08.08+1"
    assert "pending.json" not in store.files
    assert "blocked.json" not in store.files
    assert b"2026.08.08+1" in (store.launcher or b"")
    assert platform.events == [
        ("end", "2026.08.08+2"),
        ("register", "2026.08.08+2"),
        ("run", "2026.08.08+2"),
        ("health", "2026.08.08+2"),
        ("end", "2026.08.08+2"),
        ("register", "2026.08.08+1"),
        ("run", "2026.08.08+1"),
        ("health", "2026.08.08+1"),
    ]


def test_failed_rollback_blocks_future_automatic_activation(tmp_path: Path) -> None:
    first = _release(tmp_path, "2026.08.08+1", b"a")
    second = _release(tmp_path, "2026.08.08+2", b"b")
    store = _store(tmp_path)
    platform = FakePlatform()
    platform.health["2026.08.08+1"] = True
    controller = _controller(tmp_path, platform, store)
    controller.activate(release_dir=first, release_id="2026.08.08+1")
    platform.health["2026.08.08+1"] = False
    platform.health["2026.08.08+2"] = False

    with pytest.raises(WindowsActivationBlocked, match="rollback failed"):
        controller.activate(release_dir=second, release_id="2026.08.08+2")

    assert "blocked.json" in store.files
    assert "pending.json" in store.files
    with pytest.raises(WindowsActivationBlocked, match="operator recovery"):
        controller.activate(release_dir=first, release_id="2026.08.08+1")


def test_pending_recovery_conservatively_restores_previous_release(
    tmp_path: Path,
) -> None:
    first = _release(tmp_path, "2026.08.08+1", b"a")
    second = _release(tmp_path, "2026.08.08+2", b"b")
    store = _store(tmp_path)
    platform = FakePlatform()
    platform.health["2026.08.08+1"] = True
    controller = _controller(tmp_path, platform, store)
    controller.activate(release_dir=first, release_id="2026.08.08+1")
    active = json.loads(store.files["active.json"].decode("utf-8"))
    validated_second = validate_windows_release(
        release_dir=second,
        expected_release_id="2026.08.08+2",
    )
    store.files["pending.json"] = (
        json.dumps(
            {
                "schema_version": 1,
                "profile": "default",
                "candidate": {
                    "release_id": validated_second.release_id,
                    "release_digest": validated_second.release_digest,
                    "release_dir": str(validated_second.release_dir),
                },
                "previous_active": active,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    store.launcher = b"candidate-projection"
    platform.events.clear()

    recovered = controller.recover()

    assert recovered is not None
    assert recovered.active.release_id == "2026.08.08+1"
    assert "pending.json" not in store.files
    assert b"2026.08.08+1" in (store.launcher or b"")
    assert platform.events == [
        ("end", "2026.08.08+2"),
        ("register", "2026.08.08+1"),
        ("run", "2026.08.08+1"),
        ("health", "2026.08.08+1"),
    ]
