#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  printf 'usage: %s <physical-serial> <command> [args...]\n' "$0" >&2
  exit 64
fi

serial="$1"
shift

case "$serial" in
  emulator-*)
    printf 'refusing emulator serial: %s\n' "$serial" >&2
    exit 65
    ;;
esac

adb_bin="${ADB:-${ANDROID_HOME:-$HOME/Library/Android/sdk}/platform-tools/adb}"
if [[ ! -x "$adb_bin" ]]; then
  printf 'adb not executable: %s\n' "$adb_bin" >&2
  exit 69
fi

state="$($adb_bin -s "$serial" get-state 2>/dev/null || true)"
if [[ "$state" != "device" ]]; then
  printf 'physical device is not ready: serial=%s state=%s\n' "$serial" "$state" >&2
  exit 66
fi

qemu="$($adb_bin -s "$serial" shell getprop ro.kernel.qemu | tr -d '\r')"
if [[ "$qemu" == "1" ]]; then
  printf 'refusing qemu-backed device: %s\n' "$serial" >&2
  exit 65
fi

manufacturer="$($adb_bin -s "$serial" shell getprop ro.product.manufacturer | tr -d '\r')"
manufacturer_lower="$(printf '%s' "$manufacturer" | tr '[:upper:]' '[:lower:]')"
expected_manufacturer_lower="$(printf '%s' "${EXPECTED_MANUFACTURER:-}" | tr '[:upper:]' '[:lower:]')"
if [[ -n "${EXPECTED_MANUFACTURER:-}" ]] && \
   [[ "$manufacturer_lower" != "$expected_manufacturer_lower" ]]; then
  printf 'unexpected manufacturer: expected=%s actual=%s\n' \
    "$EXPECTED_MANUFACTURER" "$manufacturer" >&2
  exit 67
fi

model="$($adb_bin -s "$serial" shell getprop ro.product.model | tr -d '\r')"
android="$($adb_bin -s "$serial" shell getprop ro.build.version.release | tr -d '\r')"
printf 'physical-device-ok serial=%s manufacturer=%s model=%s android=%s\n' \
  "$serial" "$manufacturer" "$model" "$android"

export ANDROID_SERIAL="$serial"
exec "$@"
