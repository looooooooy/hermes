from __future__ import annotations

from pathlib import Path
from uuid import UUID

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.cloud.websocket_transport import (
    WebSocketsCloudTransport,
)
from hermes_connector.application.cloud_wss_client import (
    CloudClientConfig,
    CloudWSSClient,
)
from hermes_connector.bootstrap.cloud import build_cloud_wss_client
from hermes_connector.domain.local_gateway import (
    LocalRuntimeAuthority,
    ProcessIdentityEvidence,
)


class _TokenProvider:
    async def access_token(self) -> str:
        return "unused"


class _Storage:
    pass


class _RuntimeAuthority:
    async def current_runtime_authority(self) -> LocalRuntimeAuthority:
        return LocalRuntimeAuthority(
            profile="default",
            runtime_generation="runtime-1",
            instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            host_bundle_id="com.nousresearch.hermes",
            process_identity=ProcessIdentityEvidence(
                start_time_ns=1_000,
                executable_path=Path("/private/fixture/hermes-python"),
                executable_device=41,
                executable_inode=73,
            ),
            required_capabilities=("command.stream",),
            optional_capabilities=("approval.stream",),
        )


def test_bootstrap_builds_platform_independent_cloud_client_defaults() -> None:
    client = build_cloud_wss_client(
        config=CloudClientConfig(
            endpoint="wss://cloud.example.test/connector",
            tenant_id="tenant-1",
            device_id="device-1",
            connector_instance_id=UUID("11111111-1111-4111-8111-111111111111"),
            connector_version="1.2.3",
        ),
        token_provider=_TokenProvider(),
        storage=_Storage(),
        runtime_authority=_RuntimeAuthority(),
    )

    assert isinstance(client, CloudWSSClient)
    assert isinstance(client._transport, WebSocketsCloudTransport)
    assert isinstance(client._codec, ConnectorProtocolCodec)
