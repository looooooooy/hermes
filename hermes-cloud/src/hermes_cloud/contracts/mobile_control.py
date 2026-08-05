from __future__ import annotations

import json
from importlib.resources import files
from types import MappingProxyType
from typing import Final

_SOURCE = files(__package__).joinpath("generated", "mobile-control-v1.json")
_CONTRACT = json.loads(_SOURCE.read_text(encoding="utf-8"))
CONTROL_ERROR_CODES: Final = MappingProxyType(
    {str(name): int(code) for name, code in _CONTRACT["error_codes"].items()}
)
