"""Cross-module E2E imports use the three source trees, never installed copies."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for source in (
    ROOT / "hermes-agent-plugin" / "src",
    ROOT / "hermes-connector" / "src",
    ROOT / "hermes-cloud" / "src",
):
    value = str(source)
    if value not in sys.path:
        sys.path.insert(0, value)
