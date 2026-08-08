"""Read-only live Session probe used by Runtime Manager update health."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence

from hermes_connector.adapters.platform.availability import PlatformUnavailable
from hermes_connector.bootstrap.platform import select_platform_adapters
from hermes_connector.bootstrap.settings import (
    RuntimeConfigurationError,
    load_runtime_settings,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments not in {(), ("--json",)}:
        print("hermes-connector-live-session: invalid_arguments", file=sys.stderr)
        return 2
    previous_umask = os.umask(0o077)
    try:
        try:
            selected = select_platform_adapters()
            if selected.platform_name != "windows":
                raise PlatformUnavailable("live Session probe is Windows-only")
            settings = load_runtime_settings(
                environment,
                platform_name="windows",
            )
            from hermes_connector.bootstrap.windows_live_session import (
                probe_windows_live_session,
            )

            evidence = asyncio.run(probe_windows_live_session(settings))
        except (OSError, PlatformUnavailable, RuntimeConfigurationError, ValueError):
            evidence = None
        payload = {
            "live_session_ok": bool(evidence and evidence.live_session_ok),
            "runtime_generation": (
                evidence.runtime_generation if evidence is not None else None
            ),
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if payload["live_session_ok"] else 3
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
