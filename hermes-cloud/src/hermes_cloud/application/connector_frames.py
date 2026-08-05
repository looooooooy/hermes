"""Application boundary for Connector frame conformance."""

from __future__ import annotations

from hermes_cloud.domain.contract_models import CloudEnvelope
from hermes_cloud.ports.connector_frame import ConnectorFrameDecoder


class ConnectorFrameService:
    """Delegate transport decoding to the injected core-contract adapter."""

    def __init__(self, decoder: ConnectorFrameDecoder) -> None:
        self._decoder = decoder

    def decode_connector_frame(self, raw: object) -> CloudEnvelope:
        return self._decoder.decode_connector_frame(raw)
