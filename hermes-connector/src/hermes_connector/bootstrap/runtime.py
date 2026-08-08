from __future__ import annotations

import os
from collections.abc import Iterable

from hermes_connector.application.service_runner import ServiceRunner
from hermes_connector.application.supervisor import Supervisor
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.platform import (
    InstanceLockFactory,
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
    instance_lock_type: InstanceLockFactory | None = None,
) -> ServiceRunner:
    """Wire the local service without acquiring locks or starting I/O."""

    selected_lock_type = instance_lock_type
    if selected_lock_type is None:
        selected_lock_type = select_platform_adapters(platform_name).instance_lock_type
    instance_lock = selected_lock_type(
        lock_path,
        metadata_validator=metadata_validator,
    )
    supervisor = Supervisor(components, config, logger)
    return ServiceRunner(instance_lock, supervisor)
