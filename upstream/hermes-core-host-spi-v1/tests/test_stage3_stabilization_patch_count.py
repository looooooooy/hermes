"""Bound the Stage 3 stabilization patch allowance."""

from __future__ import annotations

import pytest

from tools.apply_and_verify import PatchBundle, PatchBundleError


def _lock(stage: int, patch_count: int) -> dict[str, object]:
    return {
        "stage": stage,
        "patches": [
            {"path": f"patches/{index:04d}-x.patch"}
            for index in range(1, patch_count + 1)
        ],
    }


def test_stage3_accepts_original_and_stabilized_patch_sets() -> None:
    assert PatchBundle._validated_stage(_lock(3, 3)) == 3
    assert PatchBundle._validated_stage(_lock(3, 4)) == 3


@pytest.mark.parametrize(
    ("stage", "patch_count"),
    (
        (1, 2),
        (2, 3),
        (3, 2),
        (3, 5),
    ),
)
def test_stage_patch_counts_remain_fail_closed(
    stage: int,
    patch_count: int,
) -> None:
    with pytest.raises(
        PatchBundleError,
        match="bundle patch set does not match stage",
    ):
        PatchBundle._validated_stage(_lock(stage, patch_count))
