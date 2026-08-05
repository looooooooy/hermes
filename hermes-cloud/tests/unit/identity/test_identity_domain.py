from __future__ import annotations

from dataclasses import fields

import pytest

from hermes_cloud.modules.identity.domain import (
    Argon2PasswordHasher,
    PasswordCredential,
    sha256_token_digest,
)


def test_argon2id_hasher_never_returns_or_requires_stored_plaintext() -> None:
    hasher = Argon2PasswordHasher()

    password_hash = hasher.hash("correct horse battery staple")

    assert password_hash.startswith("$argon2id$")
    assert "correct horse battery staple" not in password_hash
    assert hasher.verify(password_hash, "correct horse battery staple") is True
    assert hasher.verify(password_hash, "wrong") is False
    assert "password" not in {field.name for field in fields(PasswordCredential)}
    assert "password_hash" in {field.name for field in fields(PasswordCredential)}


def test_token_digest_is_canonical_sha256_and_rejects_empty_secret() -> None:
    digest = sha256_token_digest("opaque-refresh-token")

    assert len(digest) == 64
    assert digest == digest.lower()
    assert "opaque-refresh-token" not in digest
    with pytest.raises(ValueError, match="token"):
        sha256_token_digest("")
