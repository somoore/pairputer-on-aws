#!/usr/bin/env bash
# Re-exec under bash if invoked as `sh <script>` (uses bash-only features).
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
#
# deploy-capsule-and-rebind.sh — the CORRECT combined-deploy for a capsule change that the MCP must
# re-register against.
#
# WHY THIS EXISTS (the churn it prevents): the MCP runtime snapshots each capsule's tool registration
# ONCE at startup, binding to the capsule's release digest that is current AT THAT MOMENT. So if you
# deploy a capsule and the MCP in parallel (or you don't restart the runtime after the capsule's release
# commits), the MCP stays bound to the PRE-COMMIT release and EVERY namespaced capsule tool fails with
# "this named tool ... belongs to a superseded capsule release". The fix is a strict order:
#
#   1. deploy the capsule(s) and WAIT for each new release to commit (`/current` advances)
#   2. THEN bounce the MCP runtime so it re-snapshots against the now-current release
#      — preserving the FULL runtime config (a hand-rolled update-agent-runtime that drops
#        requestHeaderConfiguration silently breaks auth: "missing forwarded Authorization header")
#   3. verify the runtime is READY and points at the current release
#
# This script encodes exactly that. It does NOT rebuild the MCP image — use it when only the CAPSULE
# changed (bridge/rootfs/manifest). If the MCP image ALSO changed, run substrate/deploy.sh first to
# push+deploy the new MCP image, THEN this script to re-order the capsule bind.
#
# Usage:
#   substrate/deploy-capsule-and-rebind.sh <capsule-dir> [<capsule-dir> ...]
#   substrate/deploy-capsule-and-rebind.sh computer-use-desktop
#   PAIRPUTER_STACK_NAME=pairputer substrate/deploy-capsule-and-rebind.sh agent-doom computer-use-desktop
#
# Env: standard AWS credential chain (see lib/aws-env.sh). PAIRPUTER_STACK_NAME (default: pairputer).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/aws-env.sh"
hb_require_aws

STACK_NAME="${PAIRPUTER_STACK_NAME:-pairputer}"
[[ $# -ge 1 ]] || { echo "usage: $0 <capsule-dir> [<capsule-dir> ...]" >&2; exit 2; }
CAPSULES=("$@")

log(){ echo "==> $*"; }

# --- STEP 1: deploy each capsule and confirm its release committed ------------------------------------
# Plain indexed array of "cid digest" pairs (bash 3.2-safe; macOS ships no bash 4).
COMMITTED=()
for cap in "${CAPSULES[@]}"; do
  log "Deploying capsule '${cap}' (build + commit release)…"
  out="$("${SCRIPT_DIR}/deploy-capsule.sh" "${cap}" 2>&1)" || { echo "$out" >&2; echo "capsule '${cap}' deploy FAILED" >&2; exit 1; }
  echo "$out" | grep -E "Release committed|inserted" || true
  # Resolve the capsule id (deploy-capsule.sh derives it from the manifest; default = dir name).
  cid="$(python3 -c 'import yaml,sys;print((yaml.safe_load(open(sys.argv[1])).get("capsule") or {}).get("id") or sys.argv[2])' \
        "${SCRIPT_DIR}/../capsules/${cap}/capsule.yaml" "${cap}" 2>/dev/null || echo "${cap}")"
  # Read the now-current release digest so we can verify the MCP picks it up.
  digest="$(aws ssm get-parameter --name "/pairputer/capsules/${cid}/current" --query 'Parameter.Value' --output text 2>/dev/null \
            | python3 -c 'import json,sys;print(json.load(sys.stdin).get("releaseDigest") or "")' 2>/dev/null || echo "")"
  [[ -n "$digest" ]] || { echo "could not read committed release for capsule '${cid}'" >&2; exit 1; }
  COMMITTED+=("${cid} ${digest}")
  log "  ${cid} release committed: ${digest}"
done

# --- STEP 2: bounce the MCP runtime, preserving the FULL config --------------------------------------
RT="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" \
      --query 'Stacks[0].Outputs[?OutputKey==`McpRuntimeId`].OutputValue' --output text 2>/dev/null)"
[[ -n "$RT" && "$RT" != "None" ]] || { echo "could not find McpRuntimeId output on stack '${STACK_NAME}'" >&2; exit 1; }
log "Bouncing MCP runtime ${RT} so it re-snapshots tool registration against the current release…"

CFG="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RT" \
  --query '{env:environmentVariables,uri:agentRuntimeArtifact.containerConfiguration.containerUri,role:roleArn,net:networkConfiguration,proto:protocolConfiguration,auth:authorizerConfiguration,hdr:requestHeaderConfiguration}' \
  --output json)"
# Build the update spec preserving EVERYTHING (the header allowlist is the one people drop → breaks auth).
# NO nonce env var: AgentCore mints a new runtime version (and restarts the container) on ANY
# update-agent-runtime call, including an identical-config one — verified live 2026-07-13. The old
# additive PAIRPUTER_REBIND_NONCE pushed the env past AgentCore's 4000-byte cap (prod env sits at
# ~3982 bytes, dominated by the 2KB CFN-baked PAIRPUTER_CAPSULE_MANIFEST) and the bounce failed with
# ValidationException, leaving the runtime bound to the SUPERSEDED release. If it ever exists from
# that era, strip it — reclaim the headroom.
SPEC_FILE="$(mktemp)"
python3 - "$CFG" "$RT" > "$SPEC_FILE" <<'PY'
import json, sys
cfg = json.loads(sys.argv[1]); rt = sys.argv[2]
env = dict(cfg.get("env") or {}); env.pop("PAIRPUTER_REBIND_NONCE", None)
spec = {
  "agentRuntimeId": rt,
  "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": cfg["uri"]}},
  "roleArn": cfg["role"],
  "networkConfiguration": cfg["net"],
  "protocolConfiguration": cfg["proto"],
  "authorizerConfiguration": cfg["auth"],
  # PRESERVE the header allowlist — dropping it silently breaks auth ("missing forwarded Authorization").
  "requestHeaderConfiguration": cfg.get("hdr") or {"requestHeaderAllowlist": ["Authorization"]},
  "environmentVariables": env,
}
json.dump(spec, sys.stdout)
PY
# Fail-closed guard: refuse to bounce if the Authorization header would be lost.
python3 -c 'import json,sys;s=json.load(open(sys.argv[1]));h=(s.get("requestHeaderConfiguration") or {}).get("requestHeaderAllowlist") or [];sys.exit(0 if "Authorization" in h else 1)' "$SPEC_FILE" \
  || { echo "ABORT: runtime config is missing the Authorization header allowlist — refusing to bounce (would break auth)." >&2; rm -f "$SPEC_FILE"; exit 1; }

aws bedrock-agentcore-control update-agent-runtime --cli-input-json "file://${SPEC_FILE}" \
  --query '{status:status,version:agentRuntimeVersion}' --output json
rm -f "$SPEC_FILE"

# --- STEP 3: wait READY + verify the header survived and the release lines up -----------------------
log "Waiting for the runtime to reach READY…"
for _ in $(seq 1 60); do
  st="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RT" --query 'status' --output text 2>/dev/null || echo UNKNOWN)"
  case "$st" in
    UPDATING|CREATING) sleep 15 ;;
    READY) break ;;
    *) echo "runtime reached unexpected state: ${st}" >&2; exit 1 ;;
  esac
done
hdr="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RT" --query 'requestHeaderConfiguration.requestHeaderAllowlist' --output text 2>/dev/null)"
[[ "$hdr" == *Authorization* ]] || { echo "POST-CHECK FAILED: Authorization header allowlist not present after bounce." >&2; exit 1; }
log "Runtime READY with Authorization preserved. Re-registered against:"
for pair in "${COMMITTED[@]}"; do log "  ${pair% *} -> ${pair#* }"; done
log "Done. Reminder: trash any principal's stale VM (its session may still pin the old release) before the first call."
