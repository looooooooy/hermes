#!/usr/bin/env bash
set -euo pipefail

apply=false
case "${1:-}" in
  "")
    ;;
  --apply)
    apply=true
    ;;
  *)
    echo "usage: rollback.sh [--apply]" >&2
    exit 64
    ;;
esac

: "${HERMES_RELEASES_DIR:?HERMES_RELEASES_DIR is required}"
: "${HERMES_CURRENT:?HERMES_CURRENT is required}"
: "${HERMES_PREVIOUS:?HERMES_PREVIOUS is required}"

[[ -L "$HERMES_CURRENT" && -L "$HERMES_PREVIOUS" ]] || {
  echo "current and previous must both be release symlinks" >&2
  exit 78
}

current_target=$(readlink -f "$HERMES_CURRENT")
previous_target=$(readlink -f "$HERMES_PREVIOUS")
releases_root=$(readlink -f "$HERMES_RELEASES_DIR")
case "$current_target" in
  "$releases_root"/*) ;;
  *) echo "current target is outside the release directory" >&2; exit 78 ;;
esac
case "$previous_target" in
  "$releases_root"/*) ;;
  *) echo "previous target is outside the release directory" >&2; exit 78 ;;
esac

echo "rollback_current=$current_target"
echo "rollback_target=$previous_target"
if [[ "$apply" != true ]]; then
  echo "DRY RUN: no symlink was changed; rerun with --apply to switch releases"
  exit 0
fi

temporary_link="${HERMES_CURRENT}.rollback.$$"
trap 'rm -f -- "$temporary_link"' EXIT
ln -s -- "$previous_target" "$temporary_link"
mv -Tf -- "$temporary_link" "$HERMES_CURRENT"
ln -sfn -- "$current_target" "$HERMES_PREVIOUS"
trap - EXIT
echo "rollback=APPLIED restart_services_explicitly=true"
