"""Keep the cross-project test independent of either package's test config."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

for source_root in (
    REPOSITORY_ROOT / "hermes-agent-plugin" / "src",
    REPOSITORY_ROOT / "hermes-connector" / "src",
):
    source = str(source_root)
    if source not in sys.path:
        sys.path.insert(0, source)
