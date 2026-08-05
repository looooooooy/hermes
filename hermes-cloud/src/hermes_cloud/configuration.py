"""Secret-reference-only configuration for the Cloud foundation."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from hermes_cloud.errors import CloudError, ErrorCategory

MAX_SECRET_BYTES = 64 * 1024


class ConfigurationError(CloudError):
    """A configuration value is absent, direct, or unsafe to read."""

    category = ErrorCategory.CONFIGURATION
    code = "INVALID_CONFIGURATION"
    retryable = False


def _current_uid() -> int | None:
    for provider_name in ("geteuid", "getuid"):
        provider = getattr(os, provider_name, None)
        if callable(provider):
            return int(provider())
    return None


def _same_file_identity(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


@dataclass(frozen=True)
class DsnFileReference:
    """Reference to a bounded DSN file owned by the current process user."""

    path: str
    owner_uid_provider: Callable[[], int | None] = _current_uid

    def read(self) -> str:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            before_open = os.lstat(self.path)
            expected_uid = self.owner_uid_provider()
            self._validate_metadata(before_open, expected_uid)
            descriptor = os.open(self.path, flags)
            opened = os.fstat(descriptor)
            self._validate_metadata(opened, expected_uid)
            if not _same_file_identity(before_open, opened):
                raise ConfigurationError("secret reference changed before open")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                content = stream.read(MAX_SECRET_BYTES + 1)
            after_read = os.lstat(self.path)
            self._validate_metadata(after_read, expected_uid)
            if not _same_file_identity(opened, after_read):
                raise ConfigurationError("secret reference changed during read")
            if len(content) > MAX_SECRET_BYTES:
                raise ConfigurationError("secret reference exceeds size limit")
            value = content.decode("utf-8").rstrip("\r\n")
        except ConfigurationError:
            raise
        except (OSError, UnicodeError):
            raise ConfigurationError("secret reference cannot be read safely") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

        if not value:
            raise ConfigurationError("secret reference is empty")
        return value

    @staticmethod
    def _validate_metadata(
        metadata: os.stat_result,
        expected_uid: int | None,
    ) -> None:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ConfigurationError("secret reference must be a regular file")
        if expected_uid is None or metadata.st_uid != expected_uid:
            raise ConfigurationError("secret reference owner is invalid")
        permissions = stat.S_IMODE(metadata.st_mode)
        if permissions & 0o177:
            raise ConfigurationError(
                "secret reference permissions must not be wider than 0600"
            )
        if metadata.st_size > MAX_SECRET_BYTES:
            raise ConfigurationError("secret reference exceeds size limit")


class CloudConfig:
    """Validated collection of file and secret-manager references."""

    def __init__(
        self,
        dsn_files: Mapping[str, DsnFileReference],
        secret_refs: Mapping[str, str],
    ) -> None:
        self._dsn_files = dict(dsn_files)
        self._secret_refs = dict(secret_refs)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> CloudConfig:
        dsn_files: dict[str, DsnFileReference] = {}
        secret_refs: dict[str, str] = {}
        for key, value in values.items():
            if key.endswith("_DSN_FILE"):
                if not value:
                    raise ConfigurationError("DSN file reference is empty")
                dsn_files[key] = DsnFileReference(value)
                continue
            if key.endswith("_SECRET_REF"):
                if not value:
                    raise ConfigurationError("secret manager reference is empty")
                secret_refs[key] = value
                continue
            raise ConfigurationError(
                "configuration accepts only DSN files and secret references"
            )
        return cls(dsn_files, secret_refs)

    def read_dsn(self, key: str) -> str:
        reference = self._dsn_files.get(key)
        if reference is None:
            raise ConfigurationError("DSN file reference is not configured")
        return reference.read()

    def safe_summary(self) -> dict[str, list[str]]:
        return {
            "dsn_files": sorted(self._dsn_files),
            "secret_refs": sorted(self._secret_refs),
        }
