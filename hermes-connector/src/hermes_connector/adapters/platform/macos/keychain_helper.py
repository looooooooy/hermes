"""Fixed executable entrypoint for one bounded Keychain native operation."""

from hermes_connector.adapters.platform.macos.keychain_broker import (
    keychain_helper_stdio_main,
)


def main() -> None:
    keychain_helper_stdio_main()


if __name__ == "__main__":
    main()
