from base64 import urlsafe_b64encode
from hashlib import sha256

import pytest

from hermes_cloud.modules.device.http_codec import (
    decode_ed25519_public_key,
    internal_fingerprint_from_public,
    internal_revision_from_public,
    public_fingerprint_from_internal,
    public_revision_from_internal,
)


def _b64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def test_public_ed25519_key_and_fingerprint_have_one_canonical_codec() -> None:
    raw_key = bytes(range(32))
    public_key = _b64url(raw_key)
    digest = sha256(raw_key).digest()
    internal_fingerprint = digest.hex()
    public_fingerprint = f"SHA256:{_b64url(digest)}"

    assert decode_ed25519_public_key(public_key) == raw_key
    assert internal_fingerprint_from_public(public_fingerprint) == (
        internal_fingerprint
    )
    assert public_fingerprint_from_internal(internal_fingerprint) == (
        public_fingerprint
    )


@pytest.mark.parametrize(
    "value",
    (
        "",
        "A" * 42,
        "A" * 44,
        "A" * 42 + "=",
        "!" * 43,
        _b64url(bytes(range(31))),
    ),
)
def test_public_ed25519_key_codec_rejects_noncanonical_or_non_raw32(
    value: str,
) -> None:
    with pytest.raises(ValueError, match="Ed25519"):
        decode_ed25519_public_key(value)


@pytest.mark.parametrize(
    "value",
    (
        "a" * 64,
        "SHA256:" + "A" * 42,
        "SHA256:" + "A" * 44,
        "sha256:" + "A" * 43,
        "SHA256:" + "!" * 43,
    ),
)
def test_public_fingerprint_codec_never_accepts_internal_hex_or_padding(
    value: str,
) -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        internal_fingerprint_from_public(value)


def test_http_revisions_are_one_based_while_persistence_is_zero_based() -> None:
    assert public_revision_from_internal(0) == 1
    assert public_revision_from_internal(3) == 4
    assert internal_revision_from_public(1) == 0
    assert internal_revision_from_public(4) == 3

    with pytest.raises(ValueError, match="revision"):
        internal_revision_from_public(0)
    with pytest.raises(ValueError, match="revision"):
        public_revision_from_internal(-1)
