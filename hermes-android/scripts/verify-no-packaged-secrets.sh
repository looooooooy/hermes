#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_root="$project_root/app/build"
secret_file="${HOME}/.hermes/mobile-edge-initial-password.txt"
forbidden_symbol="DEBUG_INITIAL_""PASSWORD"
failure=0

report_match() {
  local category="$1"
  local path="$2"
  printf 'packaged-secret check failed: %s contains forbidden data: %s\n' \
    "$category" "${path#"$project_root"/}" >&2
  failure=1
}

scan_file() {
  local category="$1"
  local path="$2"
  local needle="$3"
  if LC_ALL=C grep -aFq -- "$needle" "$path"; then
    report_match "$category" "$path"
  fi
}

scan_archive() {
  local category="$1"
  local archive="$2"
  local needle="$3"
  local entry
  while IFS= read -r entry; do
    if unzip -p "$archive" "$entry" 2>/dev/null |
      LC_ALL=C grep -aF -- "$needle" >/dev/null; then
      report_match "$category" "$archive"
      return
    fi
  done < <(unzip -Z1 "$archive" 2>/dev/null)
}

scan_tree() {
  local category="$1"
  local root="$2"
  local needle="$3"
  local production_only="${4:-false}"
  local path
  [[ -e "$root" ]] || return
  while IFS= read -r -d '' path; do
    if [[ "$production_only" == "true" ]]; then
      case "$path" in
        *UnitTest*|*AndroidTest*)
          continue
          ;;
      esac
    fi
    case "$path" in
      *.apk|*.aab|*.jar|*.zip)
        scan_archive "$category" "$path" "$needle"
        ;;
      *)
        scan_file "$category" "$path" "$needle"
        ;;
    esac
  done < <(find "$root" -type f -print0)
}

scan_tree "production generated/classes/dex/artifact" \
  "$build_root" "$forbidden_symbol" true
scan_tree "production source" "$project_root/app/src/main" "$forbidden_symbol"
scan_tree "production source" "$project_root/app/src/debug" "$forbidden_symbol"
scan_file "production build script" "$project_root/app/build.gradle.kts" \
  "$forbidden_symbol"

if [[ -f "$secret_file" ]]; then
  packaged_secret="$(<"$secret_file")"
  if [[ -n "$packaged_secret" ]]; then
    scan_tree "production generated/classes/dex/artifact" \
      "$build_root" "$packaged_secret" true
    scan_tree "production source" "$project_root/app/src/main" "$packaged_secret"
    scan_tree "production source" "$project_root/app/src/debug" "$packaged_secret"
    scan_file "production build script" "$project_root/app/build.gradle.kts" \
      "$packaged_secret"
  fi
  unset packaged_secret
fi

if (( failure != 0 )); then
  exit 1
fi

printf 'packaged-secret check passed\n'
