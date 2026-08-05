from __future__ import annotations

import fcntl
import os
import stat
import tempfile
import unittest
from pathlib import Path

from hermes_connector.adapters.platform.macos.instance_lock import (
    AlreadyRunning,
    MacOSInstanceLock,
    UnsafeLockFile,
)


class MacOSInstanceLockTest(unittest.TestCase):
    def test_lock_file_is_private_regular_and_owned_by_current_uid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.lock"
            lock = MacOSInstanceLock(path)

            lock.acquire()
            metadata = path.lstat()

            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(metadata.st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            descriptor_flags = fcntl.fcntl(lock.fileno, fcntl.F_GETFD)
            self.assertNotEqual(descriptor_flags & fcntl.FD_CLOEXEC, 0)
            self.assertTrue(lock.is_held)
            lock.close()

    def test_nonblocking_competition_is_stable_and_never_deletes_lock_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.lock"
            owner = MacOSInstanceLock(path)
            competitor = MacOSInstanceLock(path)

            owner.acquire()
            with self.assertRaises(AlreadyRunning) as raised:
                competitor.acquire()

            self.assertEqual(
                str(raised.exception), "connector instance already running"
            )
            self.assertTrue(path.exists())
            competitor.close()
            self.assertTrue(path.exists())
            owner.close()
            self.assertTrue(path.exists())

            reused = MacOSInstanceLock(path)
            reused.acquire()
            reused.close()
            reused.close()

    def test_symlink_lock_path_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("unchanged", encoding="utf-8")
            target.chmod(0o600)
            path = root / "connector.lock"
            path.symlink_to(target)

            with self.assertRaises(UnsafeLockFile):
                MacOSInstanceLock(path).acquire()

            self.assertTrue(path.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_existing_lock_with_wide_permissions_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.lock"
            path.touch(mode=0o600)
            path.chmod(0o640)

            with self.assertRaises(UnsafeLockFile) as raised:
                MacOSInstanceLock(path).acquire()

            self.assertEqual(
                str(raised.exception), "lock file permissions must be 0600"
            )

    def test_non_regular_lock_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.lock"
            path.mkdir()

            with self.assertRaises(UnsafeLockFile) as raised:
                MacOSInstanceLock(path).acquire()

            self.assertEqual(str(raised.exception), "lock path must be a regular file")

    def test_injected_metadata_validator_can_reject_wrong_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.lock"
            observations: list[os.stat_result] = []

            def reject_owner(metadata: os.stat_result) -> None:
                observations.append(metadata)
                raise UnsafeLockFile("lock file owner is not the current uid")

            lock = MacOSInstanceLock(path, metadata_validator=reject_owner)
            with self.assertRaises(UnsafeLockFile) as raised:
                lock.acquire()

            self.assertEqual(
                str(raised.exception),
                "lock file owner is not the current uid",
            )
            self.assertEqual(len(observations), 1)
            self.assertFalse(lock.is_held)

    def test_validator_exception_closes_descriptor_and_allows_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.lock"

            def fail_validation(_: os.stat_result) -> None:
                raise RuntimeError("validator failed")

            failed = MacOSInstanceLock(path, metadata_validator=fail_validation)
            with self.assertRaisesRegex(RuntimeError, "validator failed"):
                failed.acquire()

            self.assertFalse(failed.is_held)
            recovery = MacOSInstanceLock(path)
            recovery.acquire()
            recovery.close()


if __name__ == "__main__":
    unittest.main()
