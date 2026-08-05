#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'usage: %s <vivo-physical-serial> [debug-apk] [output-directory]\n' "$0" >&2
}

activity_component_matches() {
  local output="$1"
  local package_name="$2"
  local activity_name="$3"
  local full_component="$package_name/$activity_name"
  local short_component="$package_name/.${activity_name##*.}"
  [[ "$output" == *"$full_component"* || "$output" == *"$short_component"* ]]
}

resumed_activity_file_matches() {
  local file="$1"
  local package_name="$2"
  local activity_name="$3"
  local full_component="$package_name/$activity_name"
  local short_component="$package_name/.${activity_name##*.}"
  grep -E '(mResumedActivity|ResumedActivity)' "$file" | \
    grep -F -e "$full_component" -e "$short_component" >/dev/null
}

if [[ "${1:-}" == "--self-test-activity-match" ]]; then
  activity_component_matches \
    'Activity: app.hermesmobile/.HermesVisualReviewActivity' \
    'app.hermesmobile' \
    'app.hermesmobile.HermesVisualReviewActivity'
  activity_component_matches \
    'Activity: app.hermesmobile/app.hermesmobile.HermesVisualReviewActivity' \
    'app.hermesmobile' \
    'app.hermesmobile.HermesVisualReviewActivity'
  printf 'activity component matching: ok\n'
  exit 0
fi

run_guarded() {
  local serial="$1"
  local apk="$2"
  local output_dir="$3"
  local package_name="app.hermesmobile"
  local activity_name="app.hermesmobile.HermesVisualReviewActivity"
  local component="$package_name/$activity_name"
  local measure_seconds="${MEASURE_SECONDS:-65}"
  local p95_limit_ms="${P95_LIMIT_MS:-32}"
  local jank_limit_percent="${JANK_LIMIT_PERCENT:-5}"
  local frozen_limit="${FROZEN_LIMIT:-0}"
  local minimum_frame_count="${MINIMUM_FRAME_COUNT:-300}"
  local adb_bin="${ADB:-${ANDROID_HOME:-$HOME/Library/Android/sdk}/platform-tools/adb}"

  if [[ "${EXPECTED_MANUFACTURER:-}" != "vivo" ]]; then
    printf 'EXPECTED_MANUFACTURER must be exactly vivo\n' >&2
    exit 67
  fi
  if [[ ! "$measure_seconds" =~ ^[0-9]+$ ]] || (( measure_seconds < 60 )); then
    printf 'MEASURE_SECONDS must be an integer of at least 60\n' >&2
    exit 64
  fi
  if [[ ! "$minimum_frame_count" =~ ^[0-9]+$ ]] || (( minimum_frame_count < 1 )); then
    printf 'MINIMUM_FRAME_COUNT must be a positive integer\n' >&2
    exit 64
  fi
  if [[ ! -f "$apk" ]]; then
    printf 'debug APK not found: %s\n' "$apk" >&2
    exit 66
  fi

  mkdir -p "$output_dir"

  {
    printf 'serial=%s\n' "$serial"
    printf 'manufacturer=%s\n' "$($adb_bin -s "$serial" shell getprop ro.product.manufacturer | tr -d '\r')"
    printf 'model=%s\n' "$($adb_bin -s "$serial" shell getprop ro.product.model | tr -d '\r')"
    printf 'android=%s\n' "$($adb_bin -s "$serial" shell getprop ro.build.version.release | tr -d '\r')"
    printf 'apk=%s\n' "$apk"
    printf 'apk_size_bytes=%s\n' "$(stat -f '%z' "$apk")"
    printf 'apk_sha256=%s\n' "$(shasum -a 256 "$apk" | cut -d ' ' -f 1)"
    printf 'mode=streaming-performance\n'
    printf 'measure_seconds=%s\n' "$measure_seconds"
    printf 'p95_limit_ms=%s\n' "$p95_limit_ms"
    printf 'jank_limit_percent=%s\n' "$jank_limit_percent"
    printf 'frozen_limit=%s\n' "$frozen_limit"
    printf 'minimum_frame_count=%s\n' "$minimum_frame_count"
  } > "$output_dir/device-and-run.txt"

  local install_output
  install_output="$($adb_bin -s "$serial" install -r "$apk" 2>&1)"
  printf '%s\n' "$install_output" > "$output_dir/install.txt"
  if [[ "$install_output" != *"Success"* ]]; then
    printf 'install -r failed; see %s/install.txt\n' "$output_dir" >&2
    exit 70
  fi

  "$adb_bin" -s "$serial" shell am force-stop "$package_name"
  local launch_output
  launch_output="$($adb_bin -s "$serial" shell am start -W \
    -n "$component" \
    --es mode streaming-performance 2>&1)"
  printf '%s\n' "$launch_output" > "$output_dir/launch.txt"
  if [[ "$launch_output" != *"Status: ok"* ]] || \
     ! activity_component_matches "$launch_output" "$package_name" "$activity_name"; then
    printf 'fixture launch was not confirmed; see %s/launch.txt\n' "$output_dir" >&2
    exit 71
  fi

  "$adb_bin" -s "$serial" shell dumpsys activity activities -p "$package_name" \
    > "$output_dir/activity-start.txt"
  if ! resumed_activity_file_matches \
    "$output_dir/activity-start.txt" "$package_name" "$activity_name"; then
    printf 'expected fixture Activity is not resumed; see %s/activity-start.txt\n' "$output_dir" >&2
    exit 72
  fi

  local remote_ui_dump="/data/local/tmp/hermes-streaming-performance.xml"
  "$adb_bin" -s "$serial" shell uiautomator dump "$remote_ui_dump" \
    > "$output_dir/uiautomator.txt"
  "$adb_bin" -s "$serial" pull "$remote_ui_dump" "$output_dir/window-start.xml" \
    >> "$output_dir/uiautomator.txt"
  "$adb_bin" -s "$serial" shell rm -f "$remote_ui_dump"
  if ! grep -F 'Streaming performance' "$output_dir/window-start.xml" >/dev/null; then
    printf 'streaming-performance fixture content was not visible; see %s/window-start.xml\n' \
      "$output_dir" >&2
    exit 73
  fi

  "$adb_bin" -s "$serial" exec-out screencap -p > "$output_dir/streaming-start.png"
  "$adb_bin" -s "$serial" shell dumpsys gfxinfo "$package_name" reset \
    > "$output_dir/gfxinfo-reset.txt"

  sleep "$measure_seconds"

  "$adb_bin" -s "$serial" shell dumpsys gfxinfo "$package_name" \
    > "$output_dir/gfxinfo.txt"
  "$adb_bin" -s "$serial" shell dumpsys gfxinfo "$package_name" framestats \
    > "$output_dir/gfxinfo-framestats.txt"
  "$adb_bin" -s "$serial" shell dumpsys activity activities -p "$package_name" \
    > "$output_dir/activity-end.txt"
  "$adb_bin" -s "$serial" exec-out screencap -p > "$output_dir/streaming-end.png"
  "$adb_bin" -s "$serial" shell dumpsys package "$package_name" \
    > "$output_dir/package.txt"

  if ! resumed_activity_file_matches \
    "$output_dir/activity-end.txt" "$package_name" "$activity_name"; then
    printf 'fixture Activity was not resumed at collection end; see %s/activity-end.txt\n' \
      "$output_dir" >&2
    exit 74
  fi

  python3 - \
    "$output_dir/gfxinfo.txt" \
    "$output_dir/gfxinfo-framestats.txt" \
    "$p95_limit_ms" \
    "$jank_limit_percent" \
    "$frozen_limit" \
    "$minimum_frame_count" \
    > "$output_dir/metrics.txt" <<'PY'
import csv
import math
import re
import sys

summary_path, framestats_path, p95_limit, jank_limit, frozen_limit, minimum_frames = sys.argv[1:]
p95_limit = float(p95_limit)
jank_limit = float(jank_limit)
frozen_limit = int(frozen_limit)
minimum_frames = int(minimum_frames)

summary = open(summary_path, encoding="utf-8", errors="replace").read()
framestats = open(framestats_path, encoding="utf-8", errors="replace").read().splitlines()

official_total_match = re.search(r"Total frames rendered:\s*(\d+)", summary)
official_jank_match = re.search(r"Janky frames:\s*(\d+)\s*\(([0-9.]+)%\)", summary)

durations_ms = []
header = None
for line in framestats:
    if line.startswith("Flags,"):
        header = next(csv.reader([line]))
        continue
    if header is None or not line or line.startswith("---"):
        continue
    values = next(csv.reader([line]))
    if len(values) != len(header):
        continue
    row = dict(zip(header, values))
    try:
        if int(row["Flags"]) != 0:
            continue
        duration = (int(row["FrameCompleted"]) - int(row["IntendedVsync"])) / 1_000_000.0
    except (KeyError, ValueError):
        continue
    if 0.0 <= duration < 60_000.0:
        durations_ms.append(duration)

durations_ms.sort()

def percentile(percent):
    if not durations_ms:
        return float("nan")
    index = max(0, math.ceil(percent / 100.0 * len(durations_ms)) - 1)
    return durations_ms[index]

custom_janky = sum(duration > 32.0 for duration in durations_ms)
custom_frozen = sum(duration > 700.0 for duration in durations_ms)
custom_jank_percent = custom_janky * 100.0 / len(durations_ms) if durations_ms else float("nan")
p95 = percentile(95)
verdict = (
    len(durations_ms) >= minimum_frames
    and p95 <= p95_limit
    and custom_jank_percent <= jank_limit
    and custom_frozen <= frozen_limit
)

print(f"framestats_frames={len(durations_ms)}")
print(f"minimum_frame_count={minimum_frames}")
print(f"official_total_frames={official_total_match.group(1) if official_total_match else 'unavailable'}")
print(f"official_janky_frames={official_jank_match.group(1) if official_jank_match else 'unavailable'}")
print(f"official_janky_percent={official_jank_match.group(2) if official_jank_match else 'unavailable'}")
print(f"p50_ms={percentile(50):.3f}")
print(f"p90_ms={percentile(90):.3f}")
print(f"p95_ms={p95:.3f}")
print(f"p99_ms={percentile(99):.3f}")
print(f"frames_gt_32ms={custom_janky}")
print(f"frames_gt_32ms_percent={custom_jank_percent:.3f}")
print(f"frozen_frames_gt_700ms={custom_frozen}")
print(f"acceptance={'PASS' if verdict else 'FAIL'}")
PY

  if ! grep -F 'acceptance=PASS' "$output_dir/metrics.txt" >/dev/null; then
    printf 'streaming performance thresholds failed; evidence remains in %s\n' "$output_dir" >&2
    exit 75
  fi

  printf 'saved streaming-performance evidence to %s\n' "$output_dir"
}

if [[ "${1:-}" == "--guarded" ]]; then
  if [[ $# -ne 4 ]]; then
    usage
    exit 64
  fi
  run_guarded "$2" "$3" "$4"
  exit 0
fi

if [[ $# -lt 1 || $# -gt 3 ]]; then
  usage
  exit 64
fi

serial="$1"
case "$serial" in
  emulator-*)
    printf 'refusing emulator serial: %s\n' "$serial" >&2
    exit 65
    ;;
esac

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
apk="${2:-$repo_root/app/build/outputs/apk/debug/app-debug.apk}"
output_dir="${3:-$repo_root/build/vivo-transcript-performance/$(date +%Y%m%d-%H%M%S)}"
physical_guard="$script_dir/physical-device-run.sh"

if [[ ! -f "$physical_guard" ]]; then
  printf 'physical device guard not found: %s\n' "$physical_guard" >&2
  exit 69
fi

EXPECTED_MANUFACTURER=vivo \
  bash "$physical_guard" "$serial" \
  bash "$script_dir/vivo-transcript-performance.sh" --guarded "$serial" "$apk" "$output_dir"
