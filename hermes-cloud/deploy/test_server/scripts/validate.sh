#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
release=$(cd -- "$root/../.." && pwd -P)
if [[ -n "${HERMES_RELEASES_DIR:-}" ]]; then
  releases_root=$(cd -- "$HERMES_RELEASES_DIR" 2>/dev/null && pwd -P) || {
    echo "trusted release directory is missing" >&2
    exit 78
  }
  case "$release" in
    "$releases_root"/*) ;;
    *)
      echo "validation release is outside the trusted release directory" >&2
      exit 78
      ;;
  esac
fi
validated_current_release=
if [[ -n "${HERMES_CURRENT:-}" ]]; then
  current_release=$(cd -- "$HERMES_CURRENT" 2>/dev/null && pwd -P) || {
    echo "current release is missing" >&2
    exit 78
  }
  [[ "$current_release" == "$release" ]] || {
    echo "current release does not resolve to the validation release" >&2
    exit 78
  }
  validated_current_release=$current_release
fi
release_python="$release/.venv/bin/python"
[[ -f "$release_python" && -x "$release_python" ]] || {
  echo "release virtual environment is missing Python" >&2
  exit 69
}
run_release_python() (
  unset \
    PYTHONPATH \
    PYTHONHOME \
    PYTHONUSERBASE \
    PYTHONSTARTUP \
    PYTHONINSPECT \
    PYTHONBREAKPOINT \
    PYTHONWARNINGS \
    PYTHONSAFEPATH \
    PYTHONNOUSERSITE \
    PYTHONDONTWRITEBYTECODE
  export PYTHONDONTWRITEBYTECODE=1
  exec "$release_python" -I "$@"
)
run_release_python -c \
  'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "release virtual environment requires Python 3.11 or newer" >&2
  exit 69
}
run_systemd=false
nginx_config=

while (($#)); do
  case "$1" in
    --systemd)
      run_systemd=true
      shift
      ;;
    --nginx)
      [[ $# -ge 2 ]] || {
        echo "--nginx requires the existing main nginx.conf path" >&2
        exit 64
      }
      nginx_config=$2
      shift 2
      ;;
    *)
      echo "usage: validate.sh [--systemd] [--nginx /path/to/nginx.conf]" >&2
      exit 64
      ;;
  esac
done

run_release_python -m unittest discover -s "$root/tests" -v
for script in "$root"/scripts/*.sh; do
  bash -n "$script"
done

if [[ "$run_systemd" == true ]]; then
  command -v systemd-analyze >/dev/null 2>&1 || {
    echo "systemd-analyze is unavailable" >&2
    exit 69
  }
  sqlite_systemd="$root/sqlite/systemd"
  if [[ -d "$sqlite_systemd" ]]; then
    expected_sqlite_units=(
      hermes-cloud-sqlite-business-api.service
      hermes-cloud-sqlite-connector-gateway.service
      hermes-cloud-sqlite-migrate.service
      hermes-cloud-sqlite-mint-connector-token.service
      hermes-cloud-sqlite-seed-test-data.service
    )
    shopt -s nullglob
    discovered_units=("$sqlite_systemd"/*.service)
    shopt -u nullglob
    [[ ${#discovered_units[@]} -eq ${#expected_sqlite_units[@]} ]] || {
      echo "SQLite systemd unit set is incomplete" >&2
      exit 78
    }
    systemd_units=()
    for index in "${!expected_sqlite_units[@]}"; do
      expected_unit="$sqlite_systemd/${expected_sqlite_units[$index]}"
      [[ -f "$expected_unit" && ! -L "$expected_unit" &&
        "${discovered_units[$index]}" == "$expected_unit" ]] || {
        echo "SQLite systemd unit set is incomplete" >&2
        exit 78
      }
      systemd_units+=("$expected_unit")
    done
  else
    generic_systemd="$root/systemd"
    [[ -d "$generic_systemd" ]] || {
      echo "systemd unit directory is missing" >&2
      exit 78
    }
    shopt -s nullglob
    systemd_units=("$generic_systemd"/*.service)
    shopt -u nullglob
    [[ ${#systemd_units[@]} -gt 0 ]] || {
      echo "systemd unit set is empty" >&2
      exit 78
    }
  fi
  systemd-analyze verify "${systemd_units[@]}"
fi

if [[ -n "$nginx_config" ]]; then
  command -v nginx >/dev/null 2>&1 || {
    echo "nginx is unavailable" >&2
    exit 69
  }
  nginx -t -c "$nginx_config"
fi

if [[ -n "$validated_current_release" ]]; then
  final_current_release=$(cd -- "$HERMES_CURRENT" 2>/dev/null && pwd -P) || {
    echo "current release changed during validation" >&2
    exit 78
  }
  [[ "$final_current_release" == "$validated_current_release" &&
    "$final_current_release" == "$release" ]] || {
    echo "current release changed during validation" >&2
    exit 78
  }
fi

echo "deployment_artifacts=PASS"
