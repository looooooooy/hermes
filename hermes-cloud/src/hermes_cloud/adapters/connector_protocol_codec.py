"""Connector protocol codec composition without split root-frame authority."""

from __future__ import annotations

from hermes_cloud.domain.connector_gateway import (
    ConnectorHeartbeat,
    ConnectorHello,
    ConnectorObserverEvent,
    ConnectorObserverSnapshot,
)
from hermes_cloud.domain.contract_models import CloudEnvelope
from hermes_cloud.ports.connector_frame import ConnectorFrameDecoder
from hermes_cloud.ports.connector_gateway import ConnectorProtocolCodec


class AuthoritativeFrameProtocolCodec:
    """Delegate every root-frame decode to one injected authority."""

    def __init__(
        self,
        frame_decoder: ConnectorFrameDecoder,
        protocol_codec: ConnectorProtocolCodec,
    ) -> None:
        self._frame_decoder = frame_decoder
        self._protocol_codec = protocol_codec

    def decode_connector_frame(self, raw: object) -> CloudEnvelope:
        return self._frame_decoder.decode_connector_frame(raw)

    def decode_hello(self, payload: object) -> ConnectorHello:
        return self._protocol_codec.decode_hello(payload)

    def decode_heartbeat(self, payload: object) -> ConnectorHeartbeat:
        return self._protocol_codec.decode_heartbeat(payload)

    def decode_session_snapshot(self, payload: object) -> ConnectorObserverSnapshot:
        return self._protocol_codec.decode_session_snapshot(payload)

    def decode_session_event(self, payload: object) -> ConnectorObserverEvent:
        return self._protocol_codec.decode_session_event(payload)

    def encode_connector_frame(self, envelope: CloudEnvelope) -> str:
        return self._protocol_codec.encode_connector_frame(envelope)
