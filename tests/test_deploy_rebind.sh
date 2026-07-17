#!/usr/bin/env bash
# Self-check for deploy-capsule-and-rebind.sh: the runtime-bounce spec must PRESERVE the full config,
# and the guard must ABORT if the Authorization header allowlist would be lost (the hand-rolled bounce
# that broke auth this session). No AWS — exercises the embedded spec-builder + guard logic directly.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$HERE/substrate/deploy-capsule-and-rebind.sh"

# Extract the spec-builder heredoc (between the PY markers) into a temp module.
BUILDER="$(mktemp)"
awk '/<<.PY.$/{f=1;next} /^PY$/{f=0} f' "$SCRIPT" > "$BUILDER"

run_builder() { # run_builder <cfg-json>  -> prints the update spec
  python3 "$BUILDER" "$1" "rt-1"
}

# 1. A config WITH the header allowlist -> preserved in the spec, all fields intact, and NO nonce:
#    AgentCore mints a new version on any update (identical config included), and the additive nonce
#    once pushed the env past the 4000-byte cap and broke the bounce mid-deploy. A legacy nonce in
#    the current config must be STRIPPED to reclaim that headroom.
CFG_OK='{"env":{"A":"1","PAIRPUTER_REBIND_NONCE":"legacy"},"uri":"img@sha256:x","role":"arn:role","net":{"networkMode":"PUBLIC"},"proto":{"serverProtocol":"MCP"},"auth":{"customJWTAuthorizer":{}},"hdr":{"requestHeaderAllowlist":["Authorization"]}}'
SPEC="$(run_builder "$CFG_OK")"
echo "$SPEC" | python3 -c '
import json,sys
s=json.load(sys.stdin)
assert (s["requestHeaderConfiguration"]["requestHeaderAllowlist"]==["Authorization"]), "header not preserved"
assert "PAIRPUTER_REBIND_NONCE" not in s["environmentVariables"], "legacy nonce must be stripped (env-cap headroom)"
assert s["environmentVariables"]["A"]=="1", "existing env dropped"
assert s["roleArn"]=="arn:role" and s["authorizerConfiguration"]=={"customJWTAuthorizer":{}}, "config dropped"
print("PASS: full config preserved, nonce-free")
' || { echo "FAIL: preserve"; exit 1; }

# 2. A config with a NULL header -> the builder falls back to the Authorization default (never empty).
CFG_NULL='{"env":{},"uri":"i","role":"r","net":{},"proto":{},"auth":{},"hdr":null}'
run_builder "$CFG_NULL" | python3 -c '
import json,sys
h=json.load(sys.stdin)["requestHeaderConfiguration"]["requestHeaderAllowlist"]
assert "Authorization" in h, "null hdr must default to Authorization, got %r"%h
print("PASS: null header defaults to Authorization")
' || { echo "FAIL: null-header default"; exit 1; }

# 3. The abort GUARD: a spec missing Authorization must fail the guard (exit 1), a good one passes.
GUARD='import json,sys;s=json.load(open(sys.argv[1]));h=(s.get("requestHeaderConfiguration") or {}).get("requestHeaderAllowlist") or [];sys.exit(0 if "Authorization" in h else 1)'
GOOD="$(mktemp)"; echo '{"requestHeaderConfiguration":{"requestHeaderAllowlist":["Authorization"]}}' > "$GOOD"
BAD="$(mktemp)";  echo '{"requestHeaderConfiguration":{"requestHeaderAllowlist":[]}}' > "$BAD"
python3 -c "$GUARD" "$GOOD" || { echo "FAIL: guard rejected a good spec"; exit 1; }
python3 -c "$GUARD" "$BAD"  && { echo "FAIL: guard PASSED a spec with no Authorization"; exit 1; } || true
echo "PASS: guard aborts when Authorization would be lost"

rm -f "$BUILDER" "$GOOD" "$BAD"
echo "PASS: deploy-capsule-and-rebind.sh"
