"""Port for decoding an untrusted Connector transport frame."""

from __future__ import annotations

from typing import Protocol

from hermes_cloud.domain.contract_models import CloudEnvelope


class ConnectorFrameDecoder(Protocol):
    """Validate a frame against the authoritative Cloud Envelope contract."""

    def decode_connector_frame(self, raw: object) -> CloudEnvelope:
        """Return a validated envelope or raise a body-free contract error."""
