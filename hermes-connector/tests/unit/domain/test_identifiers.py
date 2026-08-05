from __future__ import annotations

from uuid import UUID

import pytest

from hermes_connector.domain.identifiers import canonical_uuid


def test_canonical_uuid_accepts_only_lowercase_rfc4122_versions_one_to_five() -> None:
    value = "11111111-1111-4111-8111-111111111111"

    assert canonical_uuid(value) == UUID(value)
    assert canonical_uuid(UUID(value)) == UUID(value)


@pytest.mark.parametrize(
    "value",
    (
        "00000000-0000-0000-0000-000000000000",
        "11111111-1111-4111-0111-111111111111",
        "11111111-1111-0111-8111-111111111111",
        "11111111-1111-6111-8111-111111111111",
        "11111111-1111-4111-8111-11111111111A",
        "{11111111-1111-4111-8111-111111111111}",
    ),
)
def test_canonical_uuid_rejects_nil_non_rfc_bad_version_and_noncanonical(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        canonical_uuid(value)
