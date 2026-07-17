#!/usr/bin/env bash
# Re-exec under bash if invoked as `sh <script>` (uses bash-only features). Avoids a cryptic syntax error.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
#
# wire-codex.sh
# -------------
# Post-deploy Codex wiring. Reads McpEndpoint + CodexClientId from the deployed
# pairputer stack and upserts the [mcp_servers.pairputer] block into the
# local Codex config (url + oauth client_id), backing the file up first. Then
# prints the interactive login command.
#
# deploy.sh calls this at the end unless PAIRPUTER_SKIP_CODEX_CONFIG=1. It is also
# safe to run standalone anytime the endpoint or client id changes:
#
#   substrate/wire-codex.sh
#
# Env:
#   PAIRPUTER_AWS_REGION / AWS_REGION   target region (must match the deploy)
#   PAIRPUTER_STACK_NAME                default pairputer
#   PAIRPUTER_CODEX_SERVER_NAME         default pairputer
#   CODEX_CONFIG                      default ~/.codex/config.toml

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/aws-env.sh
source "${SCRIPT_DIR}/lib/aws-env.sh"
hb_require_aws || exit 1

STACK_NAME="${PAIRPUTER_STACK_NAME:-pairputer}"
SERVER_NAME="${PAIRPUTER_CODEX_SERVER_NAME:-pairputer}"
CODEX_CONFIG="${CODEX_CONFIG:-${HOME}/.codex/config.toml}"

SYNC_ONLY="false"
for arg in "$@"; do
  case "${arg}" in
    --sync-callback) SYNC_ONLY="true" ;;
    -h|--help) sed -n '4,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

output() {
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='${1}'].OutputValue | [0]" \
    --output text 2>/dev/null
}

MCP_ENDPOINT="$(output McpEndpoint)"
CLIENT_ID="$(output CodexClientId)"
USER_POOL_ID="$(output UserPoolId)"
# Fallback for stacks deployed before the UserPoolId output existed: find the pool by the client id
# (the app client belongs to exactly one pool), or by stack-name convention.
if [[ -z "${USER_POOL_ID}" || "${USER_POOL_ID}" == "None" ]] && [[ -n "${CLIENT_ID}" && "${CLIENT_ID}" != "None" ]]; then
  while IFS= read -r pool; do
    [[ -n "${pool}" ]] || continue
    if aws cognito-idp describe-user-pool-client --user-pool-id "${pool}" --client-id "${CLIENT_ID}" \
         --region "${AWS_REGION}" >/dev/null 2>&1; then
      USER_POOL_ID="${pool}"; break
    fi
  done < <(aws cognito-idp list-user-pools --max-results 60 --region "${AWS_REGION}" \
             --query "UserPools[?contains(Name, '${STACK_NAME}')].Id" --output text 2>/dev/null | tr '\t' '\n')
fi

if [[ -z "${MCP_ENDPOINT}" || "${MCP_ENDPOINT}" == "None" || -z "${CLIENT_ID}" || "${CLIENT_ID}" == "None" ]]; then
  echo "ERROR: could not read McpEndpoint/CodexClientId from stack '${STACK_NAME}' in ${AWS_REGION}." >&2
  echo "       Is the stack deployed in this region/profile?" >&2
  exit 1
fi

# Codex appends a per-server path segment to the OAuth callback, e.g.
# http://localhost:5555/callback/<hash>. That hash is stable per server-config entry but only
# materializes once Codex has attempted a login, so it can't be pre-registered at deploy time.
# This registers whatever hashed callback(s) Codex has actually used into the Cognito client's
# allow-list (keeping the bare URL too), which closes the redirect_mismatch loop automatically.
CALLBACK_BASE="${PAIRPUTER_CODEX_CALLBACK_URL:-http://localhost:5555/callback}"

# codex_callback_id: COMPUTE the exact per-server callback segment Codex appends to the redirect_uri.
# From Codex source (rmcp-client/src/perform_oauth_login.rs): the id is
#     base64url_nopad( SHA256( Url::parse(server_url).as_str() )[0..9] )   -> 12 chars
# with only the URL fragment stripped. Our McpEndpoint is already in url-crate-normalized form
# (percent-encoded, lowercase host, no fragment), so hashing it directly reproduces Codex's value
# exactly — verified against a real `codex mcp login` authorize URL. Computing it (vs. scraping Codex
# logs) means we register the CORRECT url before the user's first login, with no priming and no stale
# hashes from previous deployments.
codex_callback_id() {
  python3 - "$1" <<'PY'
import hashlib, base64, sys
url = sys.argv[1]
print(base64.urlsafe_b64encode(hashlib.sha256(url.encode()).digest()[:9]).decode().rstrip("="))
PY
}

sync_codex_callback() {
  if [[ -z "${USER_POOL_ID}" || "${USER_POOL_ID}" == "None" ]]; then
    return 1  # no pool id (older stack without the output and client-id lookup failed)
  fi
  local cid hashed_cb
  cid="$(codex_callback_id "${MCP_ENDPOINT}")" || return 1
  hashed_cb="${CALLBACK_BASE%/}/${cid}"
  echo "==> Registering Codex callback URL(s) on the Cognito client:"
  echo "      ${CALLBACK_BASE}"
  echo "      ${hashed_cb}"
  aws cognito-idp update-user-pool-client \
    --user-pool-id "${USER_POOL_ID}" --client-id "${CLIENT_ID}" --region "${AWS_REGION}" \
    --callback-urls "${CALLBACK_BASE}" "${hashed_cb}" \
    --allowed-o-auth-flows code \
    --allowed-o-auth-scopes openid "pairputer-mcp/invoke" \
    --allowed-o-auth-flows-user-pool-client \
    --supported-identity-providers COGNITO \
    >/dev/null 2>&1 && echo "    Done. Login should no longer hit redirect_mismatch." \
                    || { echo "    Could not update the client automatically." >&2; return 1; }
}

# --sync-callback: only (re)register Codex's callback URL in Cognito, then exit. Rarely needed now
# that the main path does it automatically; useful if the MCP endpoint changed or a manual re-sync.
if [[ "${SYNC_ONLY}" == "true" ]]; then
  echo "==> Syncing Codex callback URL into Cognito client ${CLIENT_ID}..."
  sync_codex_callback
  exit $?
fi

echo "==> Codex server:  ${SERVER_NAME}"
echo "==> McpEndpoint:   ${MCP_ENDPOINT}"
echo "==> CodexClientId: ${CLIENT_ID}"
echo "==> Config file:   ${CODEX_CONFIG}"

# Surgical, idempotent TOML upsert. tomllib (read-only, 3.11+) tells us the
# current values; we only rewrite the specific lines that must change, so the
# rest of a large multi-server config is left byte-for-byte intact.
SERVER_NAME="${SERVER_NAME}" \
MCP_ENDPOINT="${MCP_ENDPOINT}" \
CLIENT_ID="${CLIENT_ID}" \
CODEX_CONFIG="${CODEX_CONFIG}" \
python3 - <<'PY'
import os, sys, shutil

path = os.environ["CODEX_CONFIG"]
name = os.environ["SERVER_NAME"]
url = os.environ["MCP_ENDPOINT"]
client_id = os.environ["CLIENT_ID"]
server_hdr = f"[mcp_servers.{name}]"
oauth_hdr = f"[mcp_servers.{name}.oauth]"

os.makedirs(os.path.dirname(path), exist_ok=True)
lines = []
if os.path.exists(path):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

# Check current state with a real TOML parser (no false "changed").
try:
    import tomllib
    current = tomllib.loads("\n".join(lines)) if lines else {}
except Exception as exc:
    print(f"ERROR: {path} is not valid TOML ({exc}); not touching it.", file=sys.stderr)
    sys.exit(1)

srv = current.get("mcp_servers", {}).get(name, {})
cur_url = srv.get("url")
cur_scopes = srv.get("scopes")
cur_client = srv.get("oauth", {}).get("client_id")
cur_timeout = srv.get("tool_timeout_sec")
want_scopes = ["openid", "pairputer-mcp/invoke"]
# NOTE (measured 2026-07-08): Codex hard-caps REMOTE MCP tool calls at ~25s and this
# knob did NOT raise that ceiling in testing (AgentCore logs showed ClientDisconnect at
# exactly 25s with tool_timeout_sec=180 set). Every tool must return inside ~25s on its
# own — drive_goal is fire-and-forget for exactly this reason. The setting is kept as
# harmless documentation-of-intent / possible stdio-server benefit, nothing more.
want_timeout = 180

if cur_url == url and cur_client == client_id and cur_scopes == want_scopes and cur_timeout == want_timeout:
    print(f"==> {name} already up to date (url, scopes, oauth.client_id, tool_timeout_sec match). No change.")
    sys.exit(0)

def find_header(hdr):
    for i, ln in enumerate(lines):
        if ln.strip() == hdr:
            return i
    return -1

def section_bounds(start):
    """Range [start+1, end) of a table body, ending at the next [header]."""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j].lstrip()
        if s.startswith("[") and s.rstrip().endswith("]"):
            end = j
            break
    return start + 1, end

def upsert_key(body_start, body_end, key, value_line):
    """Replace the first `key =` line in [body_start,body_end), else insert at body_start."""
    for j in range(body_start, body_end):
        stripped = lines[j].lstrip()
        if stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="):
            lines[j] = value_line
            return body_end  # length unchanged
    lines.insert(body_start, value_line)
    return body_end + 1

url_line = f'url = "{url}"'
scopes_line = 'scopes = ["openid", "pairputer-mcp/invoke"]'
timeout_line = f'tool_timeout_sec = {want_timeout}'
client_line = f'client_id = "{client_id}"'

si = find_header(server_hdr)
if si == -1:
    # Append a fresh block at end of file.
    if lines and lines[-1].strip() != "":
        lines.append("")
    lines.append(server_hdr)
    lines.append(url_line)
    lines.append(scopes_line)
    lines.append(timeout_line)
    lines.append(oauth_hdr)
    lines.append(client_line)
else:
    bs, be = section_bounds(si)
    be = upsert_key(bs, be, "url", url_line)
    be = upsert_key(bs, be, "scopes", scopes_line)
    be = upsert_key(bs, be, "tool_timeout_sec", timeout_line)
    oi = find_header(oauth_hdr)
    if oi == -1:
        # Insert an oauth subtable right after the server block body.
        lines.insert(be, client_line)
        lines.insert(be, oauth_hdr)
    else:
        obs, obe = section_bounds(oi)
        upsert_key(obs, obe, "client_id", client_line)

# Back up before writing.
if os.path.exists(path):
    shutil.copy2(path, path + ".bak")

new_text = "\n".join(lines) + "\n"
# Validate what we're about to write parses, before clobbering.
try:
    import tomllib
    tomllib.loads(new_text)
except Exception as exc:
    print(f"ERROR: refusing to write invalid TOML ({exc}); left {path} unchanged.", file=sys.stderr)
    sys.exit(1)

with open(path, "w", encoding="utf-8") as f:
    f.write(new_text)
print(f"==> Upserted [mcp_servers.{name}] (backup at {path}.bak).")
PY

# Ensure the top-level OAuth keys Codex needs exist (append if missing; never rewrite).
# Pass CODEX_CONFIG through — this second python invocation needs it too (the first one exports it
# inline; a bare `python3 - <<PY` here would hit KeyError: 'CODEX_CONFIG').
CODEX_CONFIG="${CODEX_CONFIG}" python3 - <<'PY'
import os
path = os.environ["CODEX_CONFIG"]
try:
    import tomllib
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
except Exception:
    cfg = {}
missing = []
if "mcp_oauth_credentials_store" not in cfg:
    missing.append('mcp_oauth_credentials_store = "keyring"')
if "mcp_oauth_callback_port" not in cfg:
    missing.append("mcp_oauth_callback_port = 5555")
if "mcp_oauth_callback_url" not in cfg:
    missing.append('mcp_oauth_callback_url = "http://localhost:5555/callback"')
if missing:
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n# Added by wire-codex.sh: Codex OAuth for pairputer\n")
        f.write("\n".join(missing) + "\n")
    print("==> Added top-level Codex OAuth keys:", ", ".join(m.split(" =")[0] for m in missing))
PY

echo ""
# Close the redirect_mismatch loop automatically: compute the exact per-server callback segment Codex
# will append (SHA256 of the MCP endpoint) and register the full redirect_uri on the Cognito client.
# This makes the very FIRST `codex mcp login` succeed — no manual step, no prior login needed.
sync_codex_callback; SYNC_RC=$?

echo ""
echo "==> Codex config is wired. Complete the (interactive) login:"
echo ""
echo "      codex mcp login ${SERVER_NAME}"
echo ""
echo "    A browser opens for Cognito login; the token is stored in your OS keyring."
if [[ ${SYNC_RC} -ne 0 ]]; then
  # Only reached if the callback couldn't be auto-registered (no pool id resolvable / update failed).
  echo ""
  echo "    NOTE: the callback URL couldn't be auto-registered in Cognito. If login fails with"
  echo "    'redirect_mismatch', re-run: substrate/wire-codex.sh --sync-callback"
fi
