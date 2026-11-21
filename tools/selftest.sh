#!/usr/bin/env bash
# End-to-end self-check: builds throwaway archives, starts the web app against
# each of them and checks the bad-package/chain-switching and no-chain paths.
#
#   ./tools/selftest.sh            # uses python3 -m uvicorn
#   PYTHON=python3 ./tools/selftest.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8137}"
WORKDIR="$(mktemp -d)"
SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

start_server() {
  local archive="$1"
  ARCHIVE_DIR="$archive" "$PYTHON" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$PORT" --log-level warning >"$WORKDIR/server.log" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 50); do
    if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  echo "server did not start; log:" >&2
  cat "$WORKDIR/server.log" >&2
  return 1
}

stop_server() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
  fi
}

status_of() {
  curl -sS -o "$WORKDIR/body" -w '%{http_code}' "$1"
}

echo "== 1. 正常样例 =="
start_server "$ROOT/sample_archive"
code="$(status_of "http://127.0.0.1:$PORT/release/2026-10")"
echo "release page: HTTP $code"
[[ "$code" == "200" ]]
grep -q "目标版已核对通过" "$WORKDIR/body"
grep -q "patches/patch-09-to-10.json" "$WORKDIR/body"
code="$(status_of "http://127.0.0.1:$PORT/release/2026-10/download")"
echo "download: HTTP $code"
[[ "$code" == "200" ]]
# The downloaded bytes must hash to the manifest digest after canonicalization.
"$PYTHON" - "$ROOT" "$WORKDIR/body" "$ROOT/sample_archive/manifest.json" <<'PY'
import hashlib, json, sys
root, body_path, manifest_path = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, root)
from app.jcs import canonicalize
document = json.loads(open(body_path, "rb").read().decode("utf-8"))
manifest = json.loads(open(manifest_path, "rb").read().decode("utf-8"))
digest = hashlib.sha256(canonicalize(document)).hexdigest()
assert digest == manifest["digests"]["2026-10"], (digest, manifest["digests"]["2026-10"])
print("download canonical digest ok:", digest)
PY
stop_server

echo "== 2. 缺包 → 换链 =="
BAD="$("$PYTHON" "$ROOT/tools/make_broken_archive.py" missing-patch "$WORKDIR/missing")"
start_server "$BAD"
code="$(status_of "http://127.0.0.1:$PORT/release/2026-10")"
echo "release page: HTTP $code"
[[ "$code" == "200" ]]
grep -q "缺包" "$WORKDIR/body"
grep -q "patch-06-to-10" "$WORKDIR/body"
grep -q "已换链" "$WORKDIR/body"
code="$(status_of "http://127.0.0.1:$PORT/release/2026-10/download")"
echo "download after switch: HTTP $code"
[[ "$code" == "200" ]]
stop_server

echo "== 3. 目标版无可用链 =="
BAD="$("$PYTHON" "$ROOT/tools/make_broken_archive.py" no-chain "$WORKDIR/nolink")"
start_server "$BAD"
code="$(status_of "http://127.0.0.1:$PORT/release/2026-10")"
echo "release page: HTTP $code"
[[ "$code" == "422" ]]
grep -q "不提供下载" "$WORKDIR/body"
grep -q "均不可用" "$WORKDIR/body"
code="$(status_of "http://127.0.0.1:$PORT/release/2026-10/download")"
echo "download: HTTP $code"
[[ "$code" == "422" ]]
stop_server

echo "== 4. 底表摘要不符 → 换链 =="
BAD="$("$PYTHON" "$ROOT/tools/make_broken_archive.py" bad-bases "$WORKDIR/badbases")"
start_server "$BAD"
code="$(status_of "http://127.0.0.1:$PORT/release/2026-10")"
echo "release page: HTTP $code"
[[ "$code" == "422" ]]
grep -q "摘要不符" "$WORKDIR/body"
stop_server

echo
echo "all self-checks passed"
