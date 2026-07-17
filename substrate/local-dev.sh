#!/usr/bin/env bash
# Re-exec under bash if invoked as `sh <script>` (uses bash-only features). Avoids a cryptic syntax error.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
#
# local-dev.sh — the local capsule + MCP dev loop (roadmap F).
#
# Runs the WHOLE control+data plane on your laptop so you iterate in seconds, not the ~1h AWS cycle:
#   1. builds + runs the capsule in local Docker (ports 6902 audio / 6903 video / 6904 input /
#      6905 agent bridge / 6906 coplay state / 9000 ready) — no Lambda MicroVM;
#   2. runs the pairputer MCP server (substrate/mcp-server) in PAIRPUTER_LOCAL_MODE against that capsule —
#      VM launch/discovery, the agent bridge, and the relay all target localhost; Cognito auth is a fixed
#      dev identity. Same tools, same widget, same manifest as AWS.
#
# Then point a local MCP host at http://127.0.0.1:8000/mcp. Local mode has a fixed development identity
# and MUST NOT be exposed through a public tunnel; use the deployed OAuth-protected AWS endpoint remotely.
#
# Usage:
#   ./local-dev.sh                          # capsule = capsules/agent-doom (agent tools ON)
#   CAPSULE=hellbox-doom ./local-dev.sh     # a different capsule dir under capsules/
#   ./local-dev.sh --no-build               # reuse the last-built capsule image
#   ./local-dev.sh --capsule-only           # just run the capsule (bridge on :6905), skip the MCP server
#   ./local-dev.sh --stop                   # tear down the local capsule + MCP server
#
# Requires: docker, python3 (+ the mcp-server deps: pip install mcp), and — to reach a chat host —
# a tunnel (ngrok http 8000 / cloudflared tunnel --url http://localhost:8000).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAPSULE="${CAPSULE:-agent-doom}"
[[ "$CAPSULE" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || { echo "ERROR: invalid capsule directory name" >&2; exit 2; }
CAPSULE_DIR="${SCRIPT_DIR}/../capsules/${CAPSULE}"
IMAGE="pairputer-capsule-${CAPSULE}:local"
CONTAINER="pairputer-local-${CAPSULE}"
MCP_PORT="${PAIRPUTER_LOCAL_MCP_PORT:-8000}"
STATE_HOME="${XDG_RUNTIME_DIR:-${HOME}/.cache/pairputer}"
mkdir -p "$STATE_HOME"
chmod 0700 "$STATE_HOME"
[[ -O "$STATE_HOME" && ! -L "$STATE_HOME" ]] || { echo "ERROR: unsafe local state directory $STATE_HOME" >&2; exit 1; }
PID_FILE="${STATE_HOME}/local-mcp-${CAPSULE}.pid"
BRIDGE_KEY_FILE="${STATE_HOME}/local-bridge-${CAPSULE}.key"

DO_BUILD=1; CAPSULE_ONLY=0; STOP=0
for arg in "$@"; do
  case "$arg" in
    --no-build) DO_BUILD=0 ;;
    --capsule-only) CAPSULE_ONLY=1 ;;
    --stop) STOP=1 ;;
    -h|--help) sed -n '5,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

stop_all() {
  echo "==> Stopping local capsule + MCP server..."
  if [[ -f "$PID_FILE" && -O "$PID_FILE" && ! -L "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && ps -p "$pid" -o command= 2>/dev/null | grep -Fq "mcp-server/server.py"; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -f "$BRIDGE_KEY_FILE"
  echo "    Stopped."
}
if [[ "$STOP" == 1 ]]; then stop_all; exit 0; fi

[[ -d "$CAPSULE_DIR" ]] || { echo "ERROR: no capsule dir $CAPSULE_DIR" >&2; exit 1; }
command -v docker >/dev/null || { echo "ERROR: docker required" >&2; exit 1; }

# Derive the selected cartridge's identity and bridge from its own manifest. Defaults keep Tier 0 and
# legacy cartridges working, but there is no special "doom" registry key or fixed :6905 routing.
CAPSULE_ID="$CAPSULE"
CAPSULE_NAME="$CAPSULE"
CAPSULE_DESCRIPTION="local $CAPSULE"
BRIDGE_PORT=6905
SECCOMP_POLICY=""
LOCAL_DOCKER_CAPABILITIES=""
if [[ -f "${CAPSULE_DIR}/capsule.yaml" ]]; then
  CAPSULE_ID="$(python3 -c 'import sys,yaml;c=yaml.safe_load(open(sys.argv[1]))["capsule"];print(c.get("id") or sys.argv[2])' "${CAPSULE_DIR}/capsule.yaml" "$CAPSULE")"
  CAPSULE_NAME="$(python3 -c 'import sys,yaml;c=yaml.safe_load(open(sys.argv[1]))["capsule"];print(c.get("name") or sys.argv[2])' "${CAPSULE_DIR}/capsule.yaml" "$CAPSULE")"
  CAPSULE_DESCRIPTION="$(python3 -c 'import sys,yaml;c=yaml.safe_load(open(sys.argv[1]))["capsule"];print(c.get("description") or ("local "+sys.argv[2]))' "${CAPSULE_DIR}/capsule.yaml" "$CAPSULE")"
  BRIDGE_PORT="$(python3 -c 'import sys,yaml;c=yaml.safe_load(open(sys.argv[1]))["capsule"];print((c.get("bridge") or {}).get("port",6905))' "${CAPSULE_DIR}/capsule.yaml")"
  SECCOMP_POLICY="$(python3 -c 'import sys,yaml;c=yaml.safe_load(open(sys.argv[1]))["capsule"];print((c.get("runtime") or {}).get("localDockerSeccompProfile", ""))' "${CAPSULE_DIR}/capsule.yaml")"
  LOCAL_DOCKER_CAPABILITIES="$(python3 -c 'import sys,yaml;c=yaml.safe_load(open(sys.argv[1]))["capsule"];print("\n".join((c.get("runtime") or {}).get("localDockerCapabilities", [])))' "${CAPSULE_DIR}/capsule.yaml")"
fi
[[ "$BRIDGE_PORT" =~ ^[0-9]+$ ]] && (( BRIDGE_PORT >= 1 && BRIDGE_PORT <= 65535 )) \
  || { echo "ERROR: capsule manifest bridge.port must be 1..65535" >&2; exit 1; }

# 1. Build + run the capsule locally.
if [[ "$DO_BUILD" == 1 ]]; then
  echo "==> Building capsule '$CAPSULE' (local Docker)..."
  docker build --platform linux/arm64 -t "$IMAGE" "$CAPSULE_DIR"
fi
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
echo "==> Running capsule '$CAPSULE_ID' -> ports 6902/6903/6904/${BRIDGE_PORT}/6906/9000..."
DOCKER_SECURITY_ARGS=()
# A capsule may select a substrate-owned allowlisted profile by name. Capsule code is never executed
# directly on the developer host.
if [[ -n "$SECCOMP_POLICY" ]]; then
  SECCOMP_PROFILE="${STATE_HOME}/seccomp-${CAPSULE}.json"
  python3 "${SCRIPT_DIR}/generate-seccomp-profile.py" "$SECCOMP_POLICY" "$SECCOMP_PROFILE"
  DOCKER_SECURITY_ARGS+=(--security-opt "seccomp=${SECCOMP_PROFILE}")
fi
while IFS= read -r capability; do
  [[ -z "$capability" ]] && continue
  case "$capability" in
    NET_ADMIN|SYS_ADMIN) DOCKER_SECURITY_ARGS+=(--cap-add "$capability") ;;
    *) echo "ERROR: capsule requests unsupported local Docker capability '$capability'" >&2; exit 1 ;;
  esac
done <<< "$LOCAL_DOCKER_CAPABILITIES"
BRIDGE_CAPABILITY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
(umask 077; printf '%s\n' "$BRIDGE_CAPABILITY" > "$BRIDGE_KEY_FILE")
[[ -O "$BRIDGE_KEY_FILE" && ! -L "$BRIDGE_KEY_FILE" ]] \
  || { echo "ERROR: unsafe local bridge capability file" >&2; exit 1; }
docker run -d --name "$CONTAINER" \
  "${DOCKER_SECURITY_ARGS[@]}" \
  -e PAIRPUTER_BRIDGE_BOOTSTRAP_CAPABILITY="$BRIDGE_CAPABILITY" \
  -e PAIRPUTER_ALLOW_UID_FIREWALL=true \
  -e PAIRPUTER_DISABLE_RUN_HOOK_REKEY=true \
  -p 127.0.0.1:6902:6902 -p 127.0.0.1:6903:6903 -p 127.0.0.1:6904:6904 \
  -p "127.0.0.1:${BRIDGE_PORT}:${BRIDGE_PORT}" -p 127.0.0.1:6906:6906 \
  -p 127.0.0.1:9000:9000 -p 127.0.0.1:6901:6901 \
  "$IMAGE" >/dev/null

echo "==> Waiting for capsule ready gate (:9000/ready)..."
CAPSULE_READY=0
for _ in $(seq 1 90); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 http://127.0.0.1:9000/ready 2>/dev/null || echo 000)"
  [[ "$code" == "200" ]] && { CAPSULE_READY=1; echo "    Capsule READY."; break; }
  docker exec "$CONTAINER" test -f /run/capsule.ready >/dev/null 2>&1 \
    && { CAPSULE_READY=1; echo "    Capsule READY."; break; }
  sleep 2
done
if [[ "$CAPSULE_READY" != 1 ]]; then
  echo "ERROR: capsule did not satisfy its readiness contract within 180 seconds." >&2
  docker logs --tail 200 "$CONTAINER" >&2 || true
  exit 1
fi
# Bridge health (agent-interactive capsules only).
if curl -sf --max-time 3 -H "X-Pairputer-Bridge-Capability: ${BRIDGE_CAPABILITY}" \
  "http://127.0.0.1:${BRIDGE_PORT}/health" >/dev/null 2>&1; then
  echo "    Agent bridge :${BRIDGE_PORT} healthy."
fi

if [[ "$CAPSULE_ONLY" == 1 ]]; then
  echo "==> Capsule-only mode. Bridge on http://127.0.0.1:${BRIDGE_PORT}, audio/video/input on :6902/:6903/:6904, coplay state on :6906."
  echo "    The local bridge capability is stored mode 0600 at ${BRIDGE_KEY_FILE}."
  exit 0
fi

# 2. Run the MCP server in LOCAL MODE against the local capsule.
# Self-contained venv so the server's deps (mcp, boto3, cryptography, pyyaml) don't touch system Python.
VENV="${PAIRPUTER_LOCAL_VENV:-${HOME}/.cache/pairputer/local-mcp-venv}"
if [[ -e "$VENV" && ( ! -O "$VENV" || -L "$VENV" ) ]]; then
  echo "ERROR: unsafe local MCP venv $VENV" >&2; exit 1
fi
if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "==> Creating dev venv at ${VENV} + installing hash-locked MCP server deps..."
  python3 -m venv "$VENV"
  "${VENV}/bin/pip" install --quiet --require-hashes -r "${SCRIPT_DIR}/mcp-server/requirements.txt"
fi
PY="${VENV}/bin/python"

MANIFEST_JSON=""
if [[ -f "${CAPSULE_DIR}/capsule.yaml" ]]; then
  MANIFEST_JSON="$(python3 -c 'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))' "${CAPSULE_DIR}/capsule.yaml")"
  echo "==> Capability manifest loaded (agent tools ENABLED)."
fi
# Registry is the selected capsule's real manifest identity. Generate JSON structurally so punctuation in
# display metadata cannot break the environment value.
REGISTRY_JSON="$(CAPSULE_ID="$CAPSULE_ID" CAPSULE_NAME="$CAPSULE_NAME" CAPSULE_DESCRIPTION="$CAPSULE_DESCRIPTION" CAPSULE="$CAPSULE" python3 -c 'import json,os;cid=os.environ["CAPSULE_ID"];print(json.dumps({cid:{"arn":"local:"+os.environ["CAPSULE"],"name":os.environ["CAPSULE_NAME"],"description":os.environ["CAPSULE_DESCRIPTION"]}}))')"

echo "==> Starting MCP server (LOCAL MODE) on http://127.0.0.1:${MCP_PORT}/mcp ..."
PAIRPUTER_LOCAL_MODE=1 \
PAIRPUTER_LOCAL_CAPSULE_HOST=127.0.0.1 \
PAIRPUTER_LOCAL_BRIDGE_PORT="$BRIDGE_PORT" \
PAIRPUTER_LOCAL_BRIDGE_CAPABILITY="$BRIDGE_CAPABILITY" \
PAIRPUTER_LOCAL_AUDIO_PORT=6902 \
PAIRPUTER_LOCAL_VIDEO_PORT=6903 \
PAIRPUTER_LOCAL_INPUT_PORT=6904 \
PAIRPUTER_IMAGE_REGISTRY="$REGISTRY_JSON" \
PAIRPUTER_CAPSULE_MANIFEST="$MANIFEST_JSON" \
PAIRPUTER_VIDEO_PORT=6903 \
FASTMCP_PORT="$MCP_PORT" \
  "$PY" "${SCRIPT_DIR}/mcp-server/server.py" &
MCP_PID=$!
rm -f "$PID_FILE"
(umask 077; printf '%s\n' "$MCP_PID" > "$PID_FILE")

sleep 2
echo ""
echo "==> Local dev loop is up."
echo "    MCP endpoint:  http://127.0.0.1:${MCP_PORT}/mcp"
echo "    Capsule audio/video/input: ws://127.0.0.1:6902/:6903/:6904   agent bridge: http://127.0.0.1:${BRIDGE_PORT}   coplay state: http://127.0.0.1:6906"
echo ""
echo "    SECURITY: local mode uses a fixed dev identity. Do not expose it through ngrok/cloudflared."
echo "    For remote hosts, deploy the OAuth-protected AWS substrate."
echo ""
echo "    Stop everything:  ./local-dev.sh --stop"
echo "    (following MCP server logs; Ctrl-C leaves the capsule running)"
wait "$(cat "$PID_FILE")"
