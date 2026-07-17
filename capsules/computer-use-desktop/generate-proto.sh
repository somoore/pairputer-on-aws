#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$ROOT/rootfs/opt/capsule/desktopgen}"
mkdir -p "$OUT"
python3.11 -m grpc_tools.protoc \
  -I "$ROOT/proto" \
  --python_out="$OUT" \
  --grpc_python_out="$OUT" \
  "$ROOT/proto/pairputer/desktop/v1/desktop.proto"
find "$OUT" -type d -exec sh -c 'test -e "$1/__init__.py" || : > "$1/__init__.py"' _ {} \;
