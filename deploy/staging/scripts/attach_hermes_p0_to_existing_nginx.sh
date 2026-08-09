#!/usr/bin/env bash
set -euo pipefail

SERVER_NAME=${HERMES_SERVER_NAME:-api.seaotter.wiki}
CURRENT_LINK=/opt/hermes-cloud/current
SNIPPET_TARGET=/etc/nginx/hermes-cloud-p0.locations.conf
BACKUP_DIR=/root/hermes-cloud-nginx-backups

log() { printf '[hermes-cloud-nginx-attach] %s\n' "$*"; }
die() { printf '[hermes-cloud-nginx-attach] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "attach must run as root"
[[ "$SERVER_NAME" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid HERMES_SERVER_NAME"
command -v nginx >/dev/null 2>&1 || die "nginx is unavailable"
command -v curl >/dev/null 2>&1 || die "curl is unavailable"
[[ -L "$CURRENT_LINK" ]] || die "current release link is missing"
CURRENT_ROOT=$(readlink -f "$CURRENT_LINK")
[[ -d "$CURRENT_ROOT" ]] || die "current release target is missing"
PYTHON="$CURRENT_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || die "current release Python is unavailable"
SNIPPET_SOURCE="$CURRENT_ROOT/deploy/test_server/sqlite/nginx/hermes-test-server.conf"
[[ -f "$SNIPPET_SOURCE" && ! -L "$SNIPPET_SOURCE" ]] || die "Hermes P0 Nginx location snippet is unavailable"

for port in 8101 8102; do
  curl -fsS --max-time 3 "http://127.0.0.1:${port}/live" >/dev/null || die "Cloud live probe failed on port $port"
  curl -fsS --max-time 3 "http://127.0.0.1:${port}/ready" >/dev/null || die "Cloud ready probe failed on port $port"
done
log "Cloud readiness gate=PASS"

install -o root -g root -m 0644 "$SNIPPET_SOURCE" "$SNIPPET_TARGET"
install -d -o root -g root -m 0700 "$BACKUP_DIR"

set +e
patch_result=$(
  "$PYTHON" - "$SERVER_NAME" "$SNIPPET_TARGET" "$BACKUP_DIR" <<'PY'
from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

server_name, snippet_target, backup_dir = sys.argv[1:4]
nginx_root = Path("/etc/nginx")
include_line = f"include {snippet_target};"


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "#":
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if char in "{};":
            tokens.append(Token(char, index, index + 1))
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            start = index
            index += 1
            value: list[str] = []
            while index < length:
                char = text[index]
                if char == "\\" and index + 1 < length:
                    value.append(text[index + 1])
                    index += 2
                    continue
                if char == quote:
                    index += 1
                    break
                value.append(char)
                index += 1
            else:
                raise ValueError("unterminated quoted string")
            tokens.append(Token("".join(value), start, index))
            continue
        start = index
        while index < length:
            char = text[index]
            if char.isspace() or char in "{};#'\"":
                break
            index += 1
        if start == index:
            raise ValueError("unsupported nginx token")
        tokens.append(Token(text[start:index], start, index))
    return tokens


def server_blocks(text: str) -> list[tuple[int, int, list[list[str]]]]:
    tokens = tokenize(text)
    blocks: list[tuple[int, int, list[list[str]]]] = []
    i = 0
    while i + 1 < len(tokens):
        if tokens[i].value != "server" or tokens[i + 1].value != "{":
            i += 1
            continue
        depth = 1
        j = i + 2
        directives: list[list[str]] = []
        current: list[str] = []
        nested = 0
        while j < len(tokens) and depth:
            value = tokens[j].value
            if value == "{":
                depth += 1
                nested += 1
                current = []
            elif value == "}":
                depth -= 1
                if nested:
                    nested -= 1
                current = []
            elif value == ";" and nested == 0:
                if current:
                    directives.append(current)
                current = []
            elif nested == 0:
                current.append(value)
            j += 1
        if depth != 0:
            raise ValueError("unbalanced nginx server block")
        closing = tokens[j - 1]
        blocks.append((tokens[i].start, closing.start, directives))
        i = j
    return blocks


def directive_values(directives: list[list[str]], name: str) -> list[list[str]]:
    return [directive[1:] for directive in directives if directive and directive[0] == name]


matches: list[tuple[Path, str, int, int]] = []
for path in sorted(nginx_root.rglob("*.conf")):
    try:
        metadata = path.lstat()
    except OSError:
        continue
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        continue
    try:
        text = path.read_text(encoding="utf-8")
        blocks = server_blocks(text)
    except (OSError, UnicodeError, ValueError):
        continue
    for start, close, directives in blocks:
        names = [item for values in directive_values(directives, "server_name") for item in values]
        if server_name not in names:
            continue
        listens = [item for values in directive_values(directives, "listen") for item in values]
        has_certificate = bool(directive_values(directives, "ssl_certificate"))
        https = has_certificate or any("443" in item or item == "ssl" for item in listens)
        if https:
            matches.append((path, text, start, close))

if len(matches) != 1:
    print(f"nginx_https_match_count={len(matches)}")
    raise SystemExit(20)

path, text, start, close = matches[0]
block_text = text[start:close]
if include_line in block_text:
    print(f"target_config={path}")
    print("backup_config=none")
    print("attach_state=ALREADY_ATTACHED")
    raise SystemExit(0)

# Refuse to shadow or duplicate any pre-existing Hermes location owned by the host.
if re.search(r"(?:^|\s)location\s+(?:=\s+|\^~\s+|~\*?\s+)?/hermes(?:/|\b)", block_text):
    print(f"target_config={path}")
    print("attach_refused=existing_hermes_location")
    raise SystemExit(21)

stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
backup = Path(backup_dir) / f"{path.name}.before-hermes-{stamp}.bak"
shutil.copy2(path, backup)
os.chmod(backup, 0o600)

indent = "    "
insertion = f"\n{indent}# Hermes Cloud P0 routes (managed)\n{indent}{include_line}\n"
updated = text[:close] + insertion + text[close:]

fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(updated)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary_name, stat.S_IMODE(path.stat().st_mode))
    os.chown(temporary_name, path.stat().st_uid, path.stat().st_gid)
    os.replace(temporary_name, path)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)

print(f"target_config={path}")
print(f"backup_config={backup}")
print("attach_state=UPDATED")
PY
)
patch_status=$?
set -e
printf '%s\n' "$patch_result"

case "$patch_status" in
  0) ;;
  20) die "expected exactly one HTTPS server block for $SERVER_NAME" ;;
  21) die "existing HTTPS host already owns a /hermes location; refusing to override it" ;;
  *) die "could not safely patch existing Nginx host" ;;
esac

backup_config=$(printf '%s\n' "$patch_result" | sed -n 's/^backup_config=//p' | tail -n1)
target_config=$(printf '%s\n' "$patch_result" | sed -n 's/^target_config=//p' | tail -n1)
[[ -n "$target_config" ]] || die "patched Nginx config identity is missing"

rollback() {
  local status=$?
  if [[ $status -ne 0 && -n "${backup_config:-}" && "$backup_config" != none && -f "$backup_config" ]]; then
    cp -a "$backup_config" "$target_config"
    nginx -t >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap rollback EXIT

nginx -t
systemctl enable --now nginx >/dev/null
systemctl reload nginx

curl -kfsS --max-time 5 --resolve "$SERVER_NAME:443:127.0.0.1" \
  "https://$SERVER_NAME/hermes/live" >/dev/null || die "local HTTPS live canary failed"
curl -kfsS --max-time 5 --resolve "$SERVER_NAME:443:127.0.0.1" \
  "https://$SERVER_NAME/hermes/ready" >/dev/null || die "local HTTPS ready canary failed"

challenge='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
authorize_url="https://$SERVER_NAME/hermes/auth/native/authorize?code_challenge=$challenge&code_challenge_method=S256&redirect_uri=http%3A%2F%2F127.0.0.1%3A54321%2Foauth%2Fcallback&state=state-0123456789abcdef"
curl -kfsS --max-time 5 --resolve "$SERVER_NAME:443:127.0.0.1" "$authorize_url" \
  | grep -q 'Connect Hermes' || die "native OAuth authorize canary failed"

trap - EXIT
log "nginx config attach=PASS target=$target_config"
log "local HTTPS live/ready=PASS"
log "native OAuth authorize=PASS"
log "closure=PASS workspace_url=https://$SERVER_NAME/hermes/"
