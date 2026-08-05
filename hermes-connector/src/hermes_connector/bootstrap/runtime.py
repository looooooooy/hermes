from __future__ import annotations

import os
from collections.abc import Iterable

from hermes_connector.application.service_runner import ServiceRunner
from hermes_connector.application.supervisor import Supervisor
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.platform import (
    MetadataValidator,
    select_platform_adapters,
)
from hermes_connector.ports.component import ComponentPort
from hermes_connector.ports.logging import SafeLogPort


def build_service_runner(
    *,
    lock_path: str | os.PathLike[str],
    components: Iterable[ComponentPort],
    config: ConnectorConfig,
    logger: SafeLogPort,
    metadata_validator: MetadataValidator | None = None,
    platform_name: str | None = None,
) -> ServiceRunner:
    """Wire the local service without acquiring locks or starting I/O."""

    platform_adapters = select_platform_adapters(platform_name)
    instance_lock = platform_adapters.instance_lock_type(
        lock_path,
        metadata_validator=metadata_validator,
    )
    supervisor = Supervisor(components, config, logger)
    return ServiceRunner(instance_lock, supervisor)
