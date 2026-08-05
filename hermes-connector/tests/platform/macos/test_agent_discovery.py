from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import hermes_connector.adapters.platform.macos.agent_discovery as discovery_module
from hermes_connector.adapters.platform.macos.agent_discovery import (
    MacOSAgentDiscovery,
)

INSTANCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RUNTIME_GENERATION = "runtime-generation-1"
BUNDLE_ID = "com.nousresearch.hermes"


@dataclass(frozen=True)
class _ProcessIdentity:
    start_time_ns: int
    executable_path: Path
    executable_device: int
    executable_inode: int
    bundle_id: str


def _identity(*, start_time_ns: int = 1_000) -> _ProcessIdentity:
    executable = Path("/private/fixture/hermes-python")
    return _ProcessIdentity(
        start_time_ns=start_time_ns,
        executable_path=executable,
        executable_device=41,
        executable_inode=73,
        bundle_id=BUNDLE_ID,
    )


def _test_discovery(*args, **kwargs) -> MacOSAgentDiscovery:
    kwargs.setdefault("process_identity_provider", lambda _: _identity())
    return MacOSAgentDiscovery(*args, **kwargs)


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


class _RegistryFixture:
    def __init__(self, root: Path) -> None:
        self.registry_directory = _private_directory(root / "registry")
        self.socket_directory = _private_directory(root / "sockets")
        self.sockets: list[socket.socket] = []

    def close(self) -> None:
        for endpoint_socket in self.sockets:
            endpoint_socket.close()

    def socket_path(self, name: str = "gateway.sock") -> Path:
        path = self.socket_directory / name
        endpoint_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        endpoint_socket.bind(str(path))
        os.chmod(path, 0o600)
        self.sockets.append(endpoint_socket)
        return path

    def publish(
        self,
        *,
        name: str = "gateway-10-a.json",
        pid: int = 10,
        profile: str = "default",
        socket_path: Path | None = None,
        instance_id: str = INSTANCE_ID,
        version: int = 2,
    ) -> Path:
        endpoint_path = socket_path or self.socket_path()
        target = self.registry_directory / name
        temporary = self.registry_directory / f".{name}.tmp"
        value = {
            "version": version,
            "pid": pid,
            "profile": profile,
            "socket_path": str(endpoint_path),
            "instance_id": instance_id,
        }
        if version == 2:
            process = _identity()
            value.update(
                {
                    "runtime_generation": RUNTIME_GENERATION,
                    "process_start_time_ns": process.start_time_ns,
                    "process_executable": str(process.executable_path),
                    "process_executable_device": process.executable_device,
                    "process_executable_inode": process.executable_inode,
                    "host_bundle_id": process.bundle_id,
                }
            )
        temporary.write_text(
            json.dumps(value, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        return target


class MacOSAgentDiscoveryTest(unittest.TestCase):
    def test_v1_descriptor_is_rejected_instead_of_guessing_process_identity(
        self,
    ) -> None:
        async def scenario(root: Path) -> None:
            fixture = _RegistryFixture(root)
            discovery: MacOSAgentDiscovery | None = None
            try:
                fixture.publish(version=1)
                discovery = _test_discovery(
                    fixture.registry_directory,
                    fixture.socket_directory,
                    pid_is_alive=lambda _: True,
                )

                self.assertEqual(await discovery.discover("default"), ())
            finally:
                if discovery is not None:
                    await discovery.aclose()
                fixture.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_same_numeric_pid_reuse_during_discovery_fails_closed(self) -> None:
        async def scenario(root: Path) -> None:
            fixture = _RegistryFixture(root)
            discovery: MacOSAgentDiscovery | None = None
            try:
                descriptor = fixture.publish()
                value = json.loads(descriptor.read_text(encoding="utf-8"))
                expected = _identity()
                value.update(
                    {
                        "version": 2,
                        "runtime_generation": RUNTIME_GENERATION,
                        "process_start_time_ns": expected.start_time_ns,
                        "process_executable": str(expected.executable_path),
                        "process_executable_device": expected.executable_device,
                        "process_executable_inode": expected.executable_inode,
                        "host_bundle_id": expected.bundle_id,
                    }
                )
                descriptor.write_text(json.dumps(value), encoding="utf-8")
                descriptor.chmod(0o600)
                observed = iter((expected, _identity(start_time_ns=2_000)))
                discovery = _test_discovery(
                    fixture.registry_directory,
                    fixture.socket_directory,
                    pid_is_alive=lambda _: True,
                    process_identity_provider=lambda _: next(observed),
                )

                self.assertEqual(await discovery.discover("default"), ())
            finally:
                if discovery is not None:
                    await discovery.aclose()
                fixture.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_discovery_bounds_require_exact_positive_integers(self) -> None:
        for field, value in (
            ("max_candidates", True),
            ("max_candidates", 1.0),
            ("max_candidates", "1"),
            ("max_descriptor_bytes", True),
            ("max_descriptor_bytes", 1.0),
            ("max_descriptor_bytes", "1"),
        ):
            with (
                self.subTest(field=field, value=value),
                self.assertRaisesRegex(
                    ValueError,
                    "discovery bounds must be positive integers",
                ),
            ):
                _test_discovery(
                    Path("/tmp/registry"),
                    Path("/tmp/sockets"),
                    **{field: value},  # type: ignore[arg-type]
                )

    def test_all_directory_entries_are_bounded_and_overflow_fails_closed(
        self,
    ) -> None:
        async def scenario(root: Path) -> None:
            fixture = _RegistryFixture(root)
            discovery: MacOSAgentDiscovery | None = None
            try:
                fixture.publish()
                for index in range(64):
                    (fixture.registry_directory / f"noise-{index:03d}").write_text(
                        "ignored",
                        encoding="utf-8",
                    )
                discovery = _test_discovery(
                    fixture.registry_directory,
                    fixture.socket_directory,
                    pid_is_alive=lambda _: True,
                )

                self.assertEqual(await discovery.discover("default"), ())
            finally:
                if discovery is not None:
                    await discovery.aclose()
                fixture.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_reads_atomically_published_private_registry_by_profile(self) -> None:
        async def scenario(root: Path) -> None:
            fixture = _RegistryFixture(root)
            discovery: MacOSAgentDiscovery | None = None
            try:
                registry_path = fixture.publish()
                discovery = _test_discovery(
                    fixture.registry_directory,
                    fixture.socket_directory,
                    pid_is_alive=lambda pid: pid == 10,
                )

                endpoints = await discovery.discover("default")

                self.assertEqual(len(endpoints), 1)
                self.assertEqual(endpoints[0].pid, 10)
                self.assertEqual(endpoints[0].profile, "default")
                self.assertEqual(endpoints[0].instance_id, INSTANCE_ID)
                self.assertEqual(endpoints[0].registry_path, registry_path)
                self.assertEqual(
                    await discovery.discover("another-profile"),
                    (),
                )
            finally:
                if discovery is not None:
                    await discovery.aclose()
                fixture.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_rejects_wide_permissions_symlink_and_non_regular_registry(
        self,
    ) -> None:
        async def scenario(root: Path) -> None:
            fixture = _RegistryFixture(root)
            discovery: MacOSAgentDiscovery | None = None
            try:
                valid = fixture.publish()
                discovery = _test_discovery(
                    fixture.registry_directory,
                    fixture.socket_directory,
                    pid_is_alive=lambda _: True,
                )

                os.chmod(valid, 0o644)
                self.assertEqual(await discovery.discover("default"), ())

                valid.unlink()
                target = root / "outside.json"
                target.write_text("{}", encoding="utf-8")
                valid.symlink_to(target)
                self.assertEqual(await discovery.discover("default"), ())

                valid.unlink()
                valid.mkdir(mode=0o600)
                self.assertEqual(await discovery.discover("default"), ())
            finally:
                if discovery is not None:
                    await discovery.aclose()
                fixture.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_rejects_untrusted_parent_owner_extra_fields_and_noncanonical_uuid(
        self,
    ) -> None:
        async def scenario(root: Path) -> None:
            fixture = _RegistryFixture(root)
            wrong_owner: MacOSAgentDiscovery | None = None
            trusted: MacOSAgentDiscovery | None = None
            try:
                fixture.publish()
                wrong_owner = _test_discovery(
                    fixture.registry_directory,
                    fixture.socket_directory,
                    effective_uid=os.geteuid() + 1,
                    pid_is_alive=lambda _: True,
                )
                self.assertEqual(await wrong_owner.discover("default"), ())

                trusted = _test_discovery(
                    fixture.registry_directory,
                    fixture.socket_directory,
                    pid_is_alive=lambda _: True,
                )
                os.chmod(fixture.registry_directory, 0o755)
                self.assertEqual(await trusted.discover("default"), ())
                os.chmod(fixture.registry_directory, 0o700)

                descriptor = fixture.registry_directory / "gateway-10-a.json"
                value = json.loads(descriptor.read_text(encoding="utf-8"))
                value["platform"] = "android"
                descriptor.write_text(json.dumps(value), encoding="utf-8")
                os.chmod(descriptor, 0o600)
                self.assertEqual(await trusted.discover("default"), ())

                value.pop("platform")
                value["instance_id"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                descriptor.write_text(json.dumps(value), encoding="utf-8")
                os.chmod(descriptor, 0o600)
                self.assertEqual(await trusted.discover("default"), ())
            finally:
                if wrong_owner is not None:
                    await wrong_owner.aclose()
                if trusted is not None:
                    await trusted.aclose()
                fixture.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_rejects_stale_corrupt_duplicate_oversized_and_untrusted_socket(
        self,
    ) -> None:
        async def scenario(root: Path) -> None:
            fixture = _RegistryFixture(root)
            dead: MacOSAgentDiscovery | None = None
            live: MacOSAgentDiscovery | None = None
            try:
                path = fixture.publish()
                dead = _test_discovery(
                    fixture.registry_directory,
                    fixture.socket_directory,
                    pid_is_alive=lambda _: False,
                )
                self.assertEqual(await dead.discover("default"), ())

                path.write_bytes(
                    b'{"version":1,"version":1,"pid":10,'
                    b'"profile":"default","socket_path":"x",'
                    b'"instance_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}'
                )
                os.chmod(path, 0o600)
                live = _test_discovery(
                    fixture.registry_directory,
                    fixture.socket_directory,
                    pid_is_alive=lambda _: True,
                )
                self.assertEqual(await live.discover("default"), ())

                path.write_bytes(b"{" + b"x" * 16_384 + b"}")
                self.assertEqual(await live.discover("default"), ())

                path.unlink()
                socket_path = fixture.socket_path("wide.sock")
                os.chmod(socket_path, 0o666)
                fixture.publish(socket_path=socket_path)
                self.assertEqual(await live.discover("default"), ())

                descriptor = fixture.registry_directory / "gateway-10-a.json"
                descriptor.unlink()
                target_socket = fixture.socket_path("target.sock")
                linked_socket = fixture.socket_directory / "linked.sock"
                linked_socket.symlink_to(target_socket)
                fixture.publish(socket_path=linked_socket)
                self.assertEqual(await live.discover("default"), ())
            finally:
                if dead is not None:
                    await dead.aclose()
                if live is not None:
                    await live.aclose()
                fixture.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_candidate_overflow_fails_closed_instead_of_selecting_a_subset(
        self,
    ) -> None:
        async def scenario(root: Path) -> None:
            fixture = _RegistryFixture(root)
            discovery: MacOSAgentDiscovery | None = None
            try:
                for index, letter in enumerate(("c", "a", "b"), start=1):
                    fixture.publish(
                        name=f"gateway-{letter}.json",
                        pid=index,
                        socket_path=fixture.socket_path(f"{letter}.sock"),
                        instance_id=(
                            f"{letter * 8}-{letter * 4}-4{letter * 3}-"
                            f"8{letter * 3}-{letter * 12}"
                        ),
                    )
                discovery = _test_discovery(
                    fixture.registry_directory,
                    fixture.socket_directory,
                    max_candidates=2,
                    pid_is_alive=lambda _: True,
                )

                self.assertEqual(await discovery.discover("default"), ())
            finally:
                if discovery is not None:
                    await discovery.aclose()
                fixture.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_rejects_descriptor_mutated_in_place_during_same_fd_read(self) -> None:
        async def scenario(root: Path) -> None:
            fixture = _RegistryFixture(root)
            discovery: MacOSAgentDiscovery | None = None
            try:
                descriptor = fixture.publish()
                original_read = discovery_module._read_bounded

                def mutate_after_read(file_descriptor: int, *, maximum: int) -> bytes:
                    raw = original_read(file_descriptor, maximum=maximum)
                    with descriptor.open("ab") as mutable:
                        mutable.write(b" ")
                    return raw

                discovery = _test_discovery(
                    fixture.registry_directory,
                    fixture.socket_directory,
                    pid_is_alive=lambda _: True,
                )
                with patch.object(
                    discovery_module,
                    "_read_bounded",
                    side_effect=mutate_after_read,
                ):
                    self.assertEqual(await discovery.discover("default"), ())
            finally:
                if discovery is not None:
                    await discovery.aclose()
                fixture.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_close_joins_cancelled_inflight_worker_and_is_idempotent(self) -> None:
        async def scenario(root: Path) -> None:
            fixture = _RegistryFixture(root)
            entered = asyncio.Event()
            release = threading.Event()
            loop = asyncio.get_running_loop()

            def blocking_pid_check(_: int) -> bool:
                loop.call_soon_threadsafe(entered.set)
                release.wait()
                return True

            fixture.publish()
            discovery = _test_discovery(
                fixture.registry_directory,
                fixture.socket_directory,
                pid_is_alive=blocking_pid_check,
            )
            baseline_threads = {
                thread.ident for thread in threading.enumerate() if thread.is_alive()
            }
            operation = asyncio.create_task(discovery.discover("default"))
            await entered.wait()
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation

            first_close = asyncio.create_task(discovery.aclose())
            second_close = asyncio.create_task(discovery.aclose())
            await asyncio.sleep(0)
            self.assertFalse(first_close.done())
            self.assertFalse(second_close.done())

            first_close.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first_close
            await second_close
            await discovery.aclose()

            self.assertEqual(
                {thread.ident for thread in threading.enumerate() if thread.is_alive()},
                baseline_threads,
            )
            self.assertEqual(await discovery.discover("default"), ())
            fixture.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))


if __name__ == "__main__":
    unittest.main()
