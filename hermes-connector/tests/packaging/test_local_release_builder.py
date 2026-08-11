from __future__ import annotations

import base64
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

COMMON_PACKAGING = Path(__file__).parents[2] / "packaging" / "common"
MACOS_PACKAGING = Path(__file__).parents[2] / "packaging" / "macos"
sys.path.insert(0, str(COMMON_PACKAGING))
sys.path.insert(0, str(MACOS_PACKAGING))

from hermes_local_release import (
    ArtifactInput,
    BuildCommand,
    CommandResult,
    ReleaseBuilder,
    ReleaseBuildError,
    ReleaseInputs,
    RuntimeReleaseInput,
)
from hermes_macos_launch_agents import render_release_launch_agents


def _artifact(tmp_path: Path, name: str, content: bytes) -> ArtifactInput:
    path = tmp_path / name
    path.write_bytes(content)
    return ArtifactInput(path=path, sha256=hashlib.sha256(content).hexdigest())


def _inputs(tmp_path: Path, release_id: str = "2026.08.03-b1") -> ReleaseInputs:
    plugin_wheel = _artifact(
        tmp_path,
        "hermes_agent_plugin-1.0.0-py3-none-any.whl",
        b"plugin",
    )
    store_root = (tmp_path / "managed" / "state" / "default" / "plugin-store").resolve()
    wheel_path = (
        tmp_path
        / "releases"
        / release_id
        / "plugin"
        / "artifacts"
        / "hermes-agent-plugin"
        / "1.0.0"
        / plugin_wheel.sha256
        / plugin_wheel.path.name
    ).resolve()
    trust_store = {
        "schema_version": 1,
        "keys": [
            {
                "key_id": "plugin-release-2026",
                "signature_algorithm": "ed25519",
                "public_key": base64.b64encode(b"p" * 32).decode(),
                "not_before": "2026-01-01T00:00:00Z",
                "not_after": "2027-01-01T00:00:00Z",
            }
        ],
    }
    return ReleaseInputs(
        release_id=release_id,
        core=RuntimeReleaseInput(
            project_name="hermes-core",
            version="1.2.3",
            wheel=_artifact(tmp_path, "hermes_core-1.2.3-py3-none-any.whl", b"core"),
            lock=_artifact(tmp_path, "core.uv.lock", b"version = 1\n"),
            project=_artifact(
                tmp_path, "core.pyproject.toml", b"[project]\nname='hermes-core'\n"
            ),
            console_script="hermes",
            entrypoint="hermes_cli.main:main",
            launch_module="hermes_cli.main",
        ),
        plugin_bundle=plugin_wheel,
        plugin_store_manifest=_artifact(
            tmp_path,
            "plugin-store.json",
            json.dumps(trust_store, sort_keys=True).encode(),
        ),
        signed_plugin_manifest={
            "schema_version": 1,
            "plugin_id": "hermes-agent-plugin",
            "version": "1.0.0",
            "wheel_path": str(wheel_path),
            "wheel_sha256": plugin_wheel.sha256,
            "store_root": str(store_root),
            "entrypoint": {
                "group": "hermes_agent.plugins",
                "name": "hermes-agent-plugin",
                "value": "hermes_agent_plugin",
            },
            "signature_algorithm": "ed25519",
            "key_id": "plugin-release-2026",
            "issued_at": "2026-06-01T00:00:00Z",
            "expires_at": "2026-12-01T00:00:00Z",
            "signature": base64.b64encode(b"s" * 64).decode(),
        },
        connector=RuntimeReleaseInput(
            project_name="hermes-connector",
            version="0.1.0",
            wheel=_artifact(
                tmp_path, "hermes_connector-0.1.0-py3-none-any.whl", b"connector"
            ),
            lock=_artifact(tmp_path, "connector.uv.lock", b"version = 1\n"),
            project=_artifact(
                tmp_path,
                "connector.pyproject.toml",
                b"[project]\nname='hermes-connector'\n",
            ),
            console_script="hermes-connector",
            entrypoint="hermes_connector.cli:main",
            launch_module="hermes_connector.cli",
        ),
    )


class RecordingRunner:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.commands: list[BuildCommand] = []
        self.fail_at = fail_at

    def run(self, command: BuildCommand) -> CommandResult:
        self.commands.append(command)
        if self.fail_at == len(self.commands):
            raise RuntimeError("injected build failure")
        if command.purpose.startswith("verify-"):
            runtime = (
                "host" if command.purpose == "verify-host-runtime" else "connector"
            )
            return CommandResult(
                stdout=json.dumps(
                    {
                        "module_origin": str(
                            command.release_dir
                            / runtime
                            / "venv"
                            / "lib"
                            / "site-packages"
                        ),
                        "console_entrypoint": str(
                            command.release_dir
                            / runtime
                            / "venv"
                            / "bin"
                            / command.argv[-3]
                        ),
                    }
                )
            )
        return CommandResult(stdout="ok")


def test_dry_run_is_write_free_and_audits_isolated_locked_installs(
    tmp_path: Path,
) -> None:
    releases = tmp_path / "releases"
    builder = ReleaseBuilder(releases_root=releases, runner=RecordingRunner())

    plan = builder.build(_inputs(tmp_path), dry_run=True)

    assert not releases.exists()
    assert [command.purpose for command in plan.commands] == [
        "sync-host-dependencies",
        "install-final-core-wheel",
        "verify-host-runtime",
        "sync-connector-dependencies",
        "install-final-connector-wheel",
        "verify-connector-runtime",
    ]
    host_sync, host_install, _, connector_sync, connector_install, _ = plan.commands
    assert "--locked" in host_sync.argv and "--no-install-project" in host_sync.argv
    assert (
        "--locked" in connector_sync.argv
        and "--no-install-project" in connector_sync.argv
    )
    assert host_sync.environment["UV_PROJECT_ENVIRONMENT"].endswith("/host/venv")
    assert connector_sync.environment["UV_PROJECT_ENVIRONMENT"].endswith(
        "/connector/venv"
    )
    assert "--no-deps" in host_install.argv
    assert "--no-deps" in connector_install.argv
    assert (
        "plugin"
        not in " ".join(" ".join(command.argv) for command in plan.commands).lower()
    )


def test_build_publishes_complete_immutable_layout_and_manifest(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    runner = RecordingRunner()
    builder = ReleaseBuilder(releases_root=releases, runner=runner)

    result = builder.build(_inputs(tmp_path))

    assert result.release_dir == releases / "2026.08.03-b1"
    assert result.reused is False
    assert result.release_dir.stat().st_mode & 0o777 == 0o700
    for relative in (
        "manifest",
        "host/venv",
        "plugin/artifacts",
        "plugin/metadata",
        "connector/venv",
        "services",
        "receipts",
    ):
        assert (result.release_dir / relative).is_dir()
    assert not any(
        path.name.startswith(".") and "staging" in path.name
        for path in releases.iterdir()
    )
    manifest = json.loads(
        (result.release_dir / "manifest" / "release.json").read_text()
    )
    assert (
        result.release_dir / "manifest" / "release.json"
    ).stat().st_mode & 0o777 == 0o600
    assert all(".staging." not in " ".join(command.argv) for command in result.commands)
    assert manifest["release_id"] == "2026.08.03-b1"
    assert manifest["release_digest"] == result.release_digest
    assert (
        manifest["signed_plugin_manifest"] == _inputs(tmp_path).signed_plugin_manifest
    )
    assert manifest["verification"]["host"]["module_origin"].startswith(
        str(result.release_dir / "host" / "venv")
    )
    assert manifest["verification"]["connector"]["console_entrypoint"].startswith(
        str(result.release_dir / "connector" / "venv")
    )
    assert (
        result.release_dir
        / "plugin"
        / "artifacts"
        / "hermes-agent-plugin"
        / "1.0.0"
        / _inputs(tmp_path).plugin_bundle.sha256
        / "hermes_agent_plugin-1.0.0-py3-none-any.whl"
    ).read_bytes() == b"plugin"
    plugin_wheel = Path(manifest["signed_plugin_manifest"]["wheel_path"])
    assert plugin_wheel.stat().st_mode & 0o777 == 0o400
    assert plugin_wheel.parent.stat().st_mode & 0o222 == 0
    for runtime in ("core", "connector"):
        wheel = (
            result.release_dir
            / "receipts"
            / "inputs"
            / runtime
            / manifest[runtime]["wheel_path"]
        )
        assert wheel.stat().st_mode & 0o777 == 0o400
        assert wheel.parent.stat().st_mode & 0o222 == 0
    assert (
        result.release_dir / "plugin" / "metadata" / "signed-plugin-manifest.json"
    ).stat().st_mode & 0o777 == 0o400
    assert (
        result.release_dir / "plugin" / "metadata" / "trust-store.json"
    ).stat().st_mode & 0o777 == 0o400
    assert not (result.release_dir / "plugin" / "store").exists()
    receipt_path = result.release_dir / "receipts" / "build-commands.json"
    receipt = json.loads(receipt_path.read_text())
    assert [item["status"] for item in receipt["commands"]] == ["succeeded"] * 6
    assert ".staging." not in receipt_path.read_text()
    assert all("stdout" not in item for item in receipt["commands"])
    assert (
        manifest["receipts"]["build_commands_sha256"]
        == hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    )


def test_macos_service_templates_are_part_of_atomic_release_and_digest(
    tmp_path: Path,
) -> None:
    releases = tmp_path / "releases"
    builder = ReleaseBuilder(
        releases_root=releases,
        runner=RecordingRunner(),
        service_renderer=render_release_launch_agents,
    )

    result = builder.build(_inputs(tmp_path))

    for name in ("host", "connector"):
        path = result.release_dir / "services" / f"com.hermes.{name}.plist"
        assert path.is_file()
        assert path.stat().st_mode & 0o777 == 0o600
        assert str(result.release_dir).encode() in path.read_bytes()
        assert b".staging." not in path.read_bytes()
    manifest = json.loads(
        (result.release_dir / "manifest" / "release.json").read_text()
    )
    assert set(manifest["services"]) == {
        "com.hermes.connector.plist",
        "com.hermes.host.plist",
    }


def test_identical_release_is_idempotent_but_digest_conflict_fails_closed(
    tmp_path: Path,
) -> None:
    releases = tmp_path / "releases"
    first_runner = RecordingRunner()
    first = ReleaseBuilder(releases_root=releases, runner=first_runner).build(
        _inputs(tmp_path)
    )

    second_runner = RecordingRunner()
    second = ReleaseBuilder(releases_root=releases, runner=second_runner).build(
        _inputs(tmp_path)
    )

    assert second.reused is True
    assert second.release_digest == first.release_digest
    assert second_runner.commands == []

    changed = _inputs(tmp_path)
    changed_manifest = dict(changed.signed_plugin_manifest)
    changed_manifest["signature"] = base64.b64encode(b"x" * 64).decode()
    changed = replace(changed, signed_plugin_manifest=changed_manifest)
    with pytest.raises(
        ReleaseBuildError, match="release id already exists with a different digest"
    ):
        ReleaseBuilder(releases_root=releases, runner=RecordingRunner()).build(changed)


def test_idempotency_rejects_tampered_published_artifacts(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    inputs = _inputs(tmp_path)
    ReleaseBuilder(releases_root=releases, runner=RecordingRunner()).build(inputs)
    published_plugin = Path(inputs.signed_plugin_manifest["wheel_path"])
    published_plugin.chmod(0o600)
    published_plugin.write_bytes(b"tampered")

    with pytest.raises(ReleaseBuildError, match="published artifact digest mismatch"):
        ReleaseBuilder(releases_root=releases, runner=RecordingRunner()).build(inputs)


def test_partial_build_never_publishes_and_cleans_staging(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    builder = ReleaseBuilder(releases_root=releases, runner=RecordingRunner(fail_at=2))

    with pytest.raises(ReleaseBuildError, match="injected build failure"):
        builder.build(_inputs(tmp_path))

    assert not (releases / "2026.08.03-b1").exists()
    assert not list(releases.glob(".*.staging.*"))


def test_windows_staging_path_stays_below_legacy_max_path(tmp_path: Path) -> None:
    captured: list[Path] = []

    class CapturingBuilder(ReleaseBuilder):
        def _prepare_staging(self, staging, inputs, services):
            captured.append(staging)
            raise ReleaseBuildError("captured staging path")

    release_id = "desktop-0.1.0-windows-x86_64"
    with pytest.raises(ReleaseBuildError, match="captured staging path"):
        CapturingBuilder(
            releases_root=tmp_path / "releases", runner=RecordingRunner()
        ).build(_inputs(tmp_path, release_id=release_id))

    plugin_wheel = _inputs(tmp_path, release_id=release_id).plugin_bundle
    windows_path = (
        "C:\\Users\\runneradmin\\AppData\\Local\\Hermes\\releases\\"
        f"{captured[0].name}\\plugin\\artifacts\\hermes-agent-plugin\\1.0.0\\"
        f"{plugin_wheel.sha256}\\{plugin_wheel.path.name}"
    )
    assert len(windows_path) < 260


@pytest.mark.parametrize(
    "release_id", ["../escape", "/absolute", "two/parts", ".", "..", " bad"]
)
def test_release_id_cannot_escape_versioned_directory(
    tmp_path: Path, release_id: str
) -> None:
    with pytest.raises(ReleaseBuildError, match="release_id"):
        ReleaseBuilder(
            releases_root=tmp_path / "releases", runner=RecordingRunner()
        ).build(_inputs(tmp_path, release_id=release_id), dry_run=True)


def test_rejects_sha_mismatch_symlink_and_symlinked_release_root(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    bad = ArtifactInput(path=inputs.core.wheel.path, sha256="0" * 64)
    with pytest.raises(ReleaseBuildError, match="sha256 mismatch"):
        ReleaseBuilder(
            releases_root=tmp_path / "releases", runner=RecordingRunner()
        ).build(replace(inputs, core=replace(inputs.core, wheel=bad)), dry_run=True)

    target = tmp_path / "actual-plugin.tar"
    target.write_bytes(b"plugin")
    link = tmp_path / "linked-plugin.tar"
    link.symlink_to(target)
    linked = ArtifactInput(path=link, sha256=hashlib.sha256(b"plugin").hexdigest())
    with pytest.raises(ReleaseBuildError, match="symlink"):
        ReleaseBuilder(
            releases_root=tmp_path / "releases", runner=RecordingRunner()
        ).build(replace(inputs, plugin_bundle=linked), dry_run=True)

    actual_root = tmp_path / "actual-releases"
    actual_root.mkdir()
    linked_root = tmp_path / "linked-releases"
    linked_root.symlink_to(actual_root, target_is_directory=True)
    with pytest.raises(ReleaseBuildError, match="symlink"):
        ReleaseBuilder(releases_root=linked_root, runner=RecordingRunner()).build(
            inputs, dry_run=True
        )


def test_rejects_editable_or_direct_url_sources(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    editable_lock = _artifact(
        tmp_path,
        "bad.uv.lock",
        b'[[package]]\nname = "dependency"\nsource = { editable = "../source" }\n',
    )
    with pytest.raises(ReleaseBuildError, match="editable"):
        ReleaseBuilder(
            releases_root=tmp_path / "releases", runner=RecordingRunner()
        ).build(
            replace(inputs, core=replace(inputs.core, lock=editable_lock)), dry_run=True
        )

    direct_project = _artifact(
        tmp_path,
        "bad.pyproject.toml",
        b'dependencies = ["thing @ file:///tmp/thing"]\n',
    )
    with pytest.raises(ReleaseBuildError, match="direct URL"):
        ReleaseBuilder(
            releases_root=tmp_path / "releases", runner=RecordingRunner()
        ).build(
            replace(
                inputs, connector=replace(inputs.connector, project=direct_project)
            ),
            dry_run=True,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("signature_algorithm", "rsa", "signature_algorithm"),
        ("signature", base64.b64encode(b"short").decode(), "signature"),
        ("wheel_sha256", "f" * 64, "wheel_sha256"),
        ("key_id", "unknown-key", "trusted key"),
        ("issued_at", "2027-02-01T00:00:00Z", "validity window"),
    ],
)
def test_rejects_invalid_plugin_signature_declaration(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    inputs = _inputs(tmp_path)
    declaration = dict(inputs.signed_plugin_manifest)
    declaration[field] = value

    with pytest.raises(ReleaseBuildError, match=message):
        ReleaseBuilder(
            releases_root=tmp_path / "releases", runner=RecordingRunner()
        ).build(replace(inputs, signed_plugin_manifest=declaration), dry_run=True)


def test_rejects_plugin_artifact_path_outside_release_or_store_root_inside_release(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    declaration = dict(inputs.signed_plugin_manifest)
    declaration["wheel_path"] = str((tmp_path / "other-wheel.whl").resolve())

    with pytest.raises(ReleaseBuildError, match="wheel_path"):
        ReleaseBuilder(
            releases_root=tmp_path / "releases", runner=RecordingRunner()
        ).build(replace(inputs, signed_plugin_manifest=declaration), dry_run=True)

    declaration = dict(inputs.signed_plugin_manifest)
    declaration["store_root"] = str(
        (tmp_path / "releases" / inputs.release_id / "plugin" / "state").resolve()
    )
    with pytest.raises(ReleaseBuildError, match="outside immutable release"):
        ReleaseBuilder(
            releases_root=tmp_path / "releases", runner=RecordingRunner()
        ).build(replace(inputs, signed_plugin_manifest=declaration), dry_run=True)


def test_verification_cannot_report_origins_outside_its_venv(tmp_path: Path) -> None:
    class EscapingRunner(RecordingRunner):
        def run(self, command: BuildCommand) -> CommandResult:
            if command.purpose == "verify-host-runtime":
                return CommandResult(
                    stdout=json.dumps(
                        {
                            "module_origin": "/tmp/source/hermes/__init__.py",
                            "console_entrypoint": "/tmp/source/hermes",
                        }
                    )
                )
            return super().run(command)

    with pytest.raises(ReleaseBuildError, match="outside isolated venv"):
        ReleaseBuilder(
            releases_root=tmp_path / "releases", runner=EscapingRunner()
        ).build(_inputs(tmp_path))
    assert not (tmp_path / "releases" / "2026.08.03-b1").exists()
