#!/usr/bin/env bash
# Re-exec under bash if invoked as `sh <script>` (uses bash-only features). Avoids a cryptic syntax error.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
#
# wire-chatgpt.sh
# ---------------
# Post-deploy ChatGPT connector wiring + verification. Unlike Codex there is no local config file to
# write; ChatGPT (web) is configured in its UI. This script:
#   1. reads McpEndpoint + ChatGPTClientId from the deployed pairputer stack;
#   2. verifies the MCP auth discovery chain ChatGPT depends on (RFC 9728 protected-resource metadata
#      on the AgentCore endpoint -> Cognito OIDC discovery);
#   3. prints the exact Developer-mode connector setup steps;
#   4. with --register-callback <url>: registers the per-connector callback URL ChatGPT displays at
#      creation (https://chatgpt.com/connector/oauth/<id>) on the Cognito ChatGPT client — the same
#      post-deploy registration wire-codex.sh does for Codex's hashed localhost callback.
#
#   substrate/wire-chatgpt.sh                                    # verify + print setup steps
#   substrate/wire-chatgpt.sh --register-callback <url>          # after creating the connector
#
# Env:
#   PAIRPUTER_AWS_REGION / AWS_REGION   target region (must match the deploy)
#   PAIRPUTER_STACK_NAME                default pairputer

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/aws-env.sh
source "${SCRIPT_DIR}/lib/aws-env.sh"
hb_require_aws || exit 1

STACK_NAME="${PAIRPUTER_STACK_NAME:-pairputer}"

REGISTER_URL=""
while [[ $# -gt 0 ]]; do
  case "${1}" in
    --register-callback) REGISTER_URL="${2:-}"; shift 2 ;;
    --stack) STACK_NAME="${2:-}"; shift 2 ;;
    -h|--help) sed -n '4,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: ${1}" >&2; exit 2 ;;
  esac
done

output() {
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='${1}'].OutputValue | [0]" \
    --output text 2>/dev/null
}

MCP_ENDPOINT="$(output McpEndpoint)"
CLIENT_ID="$(output ChatGPTClientId)"
USER_POOL_ID="$(output UserPoolId)"

if [[ -z "${MCP_ENDPOINT}" || "${MCP_ENDPOINT}" == "None" || -z "${CLIENT_ID}" || "${CLIENT_ID}" == "None" ]]; then
  echo "ERROR: could not read McpEndpoint/ChatGPTClientId from stack '${STACK_NAME}' in ${AWS_REGION}." >&2
  echo "       Is the stack deployed (with the multi-host identity update) in this region/profile?" >&2
  exit 1
fi

# --register-callback: add the per-connector callback ChatGPT displayed at creation time. Keeps the
# legacy static URL too (harmless; covers published apps). Mirrors wire-codex.sh's registration.
if [[ -n "${REGISTER_URL}" ]]; then
  case "${REGISTER_URL}" in
    https://chatgpt.com/*) ;;
    *) echo "ERROR: expected an https://chatgpt.com/... callback URL, got: ${REGISTER_URL}" >&2; exit 2 ;;
  esac
  echo "==> Registering ChatGPT callback URL(s) on Cognito client ${CLIENT_ID}:"
  echo "      https://chatgpt.com/connector_platform_oauth_redirect"
  echo "      ${REGISTER_URL}"
  # Preserve any previously registered per-connector callbacks (multiple connector instances).
  # while-read, not mapfile: macOS ships bash 3.2.
  URLS=("https://chatgpt.com/connector_platform_oauth_redirect" "${REGISTER_URL}")
  while IFS= read -r u; do
    [[ -n "${u}" && "${u}" != "${URLS[0]}" && "${u}" != "${URLS[1]}" ]] && URLS+=("${u}")
  done < <(aws cognito-idp describe-user-pool-client \
    --user-pool-id "${USER_POOL_ID}" --client-id "${CLIENT_ID}" --region "${AWS_REGION}" \
    --query 'UserPoolClient.CallbackURLs[]' --output text 2>/dev/null | tr '\t' '\n')
  # NOTE the scope list: ChatGPT's connect flow requests EVERY scope Cognito advertises
  # (openid email phone profile), so the client must allow the full standard OIDC set + our scope or
  # it invalid_scope-bounces (wall). Narrowing this to "openid pairputer-mcp/invoke" re-breaks OAuth.
  aws cognito-idp update-user-pool-client \
    --user-pool-id "${USER_POOL_ID}" --client-id "${CLIENT_ID}" --region "${AWS_REGION}" \
    --callback-urls "${URLS[@]}" \
    --allowed-o-auth-flows code \
    --allowed-o-auth-scopes openid email phone profile "pairputer-mcp/invoke" \
    --allowed-o-auth-flows-user-pool-client \
    --supported-identity-providers COGNITO \
    --explicit-auth-flows ALLOW_REFRESH_TOKEN_AUTH ALLOW_USER_SRP_AUTH \
    >/dev/null && echo "    Done. Retry Connect in ChatGPT." \
               || { echo "    Could not update the client." >&2; exit 1; }
  exit 0
fi

echo "==> McpEndpoint:      ${MCP_ENDPOINT}"
echo "==> ChatGPTClientId:  ${CLIENT_ID}"
echo ""
echo "==> Verifying the MCP auth discovery chain ChatGPT depends on..."

# 1. AgentCore must 401 with a resource_metadata pointer (RFC 9728). Proven on the live stack
#    2026-07-08 (docs/hosts/README.md PROBE-4); re-verified here for THIS stack.
WWW_AUTH="$(curl -sS -o /dev/null -D - -X POST "${MCP_ENDPOINT}" \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
  | tr -d '\r' | grep -i '^www-authenticate:' || true)"
if [[ "${WWW_AUTH}" == *resource_metadata=* ]]; then
  echo "    [ok] 401 carries WWW-Authenticate resource_metadata"
else
  echo "    [FAIL] MCP endpoint 401 lacks resource_metadata — ChatGPT cannot discover auth." >&2
  exit 1
fi

# 2. The protected-resource metadata document must resolve and name the Cognito issuer.
PRM_URL="$(printf '%s' "${WWW_AUTH}" | sed -n 's/.*resource_metadata="\([^"]*\)".*/\1/p')"
PRM="$(curl -sS "${PRM_URL}" || true)"
if [[ "${PRM}" == *authorization_servers* && "${PRM}" == *cognito-idp* ]]; then
  echo "    [ok] protected-resource metadata resolves -> Cognito issuer"
else
  echo "    [FAIL] protected-resource metadata missing/wrong: ${PRM_URL}" >&2
  exit 1
fi

# 3. Cognito's OIDC discovery must resolve (the RFC 8414 path 400s on Cognito; OIDC fallback is
#    accepted per the MCP spec + OpenAI Apps SDK auth docs).
ISSUER="$(printf '%s' "${PRM}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["authorization_servers"][0])')"
if curl -sSf "${ISSUER}/.well-known/openid-configuration" >/dev/null; then
  echo "    [ok] Cognito OIDC discovery resolves (${ISSUER})"
else
  echo "    [FAIL] Cognito OIDC discovery unreachable: ${ISSUER}" >&2
  exit 1
fi

cat <<EOF

==> Discovery chain verified. Connect ChatGPT (Developer mode):

    1. ChatGPT (web) -> Settings -> Apps & Connectors -> Advanced settings -> enable Developer mode.
    2. Settings -> Apps & Connectors -> Create (new connector / app):
         MCP server URL:   ${MCP_ENDPOINT}
         Authentication:   OAuth
         Client ID:        ${CLIENT_ID}
         (public PKCE client - no client secret)
    3. ChatGPT shows this connector's OAuth callback URL (https://chatgpt.com/connector/oauth/...).
       Register it on the Cognito client:

         substrate/wire-chatgpt.sh --register-callback '<that url>'

    4. Back in ChatGPT, click Connect and sign in with your pairputer admin credentials.
    5. In a chat: enable the connector, then ask ChatGPT to "open pairputer" / run play_capsule.

    Once linked on web, the connector is available in the ChatGPT desktop app too.
EOF
