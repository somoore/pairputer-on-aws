#!/usr/bin/env bash
# Re-exec under bash if invoked as `sh <script>` (uses bash-only features). Avoids a cryptic syntax error.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
#
# wire-claude.sh (STUB host — see docs/hosts/claude.md)
# ------------------------------------------------------
# Claude connector setup printer + discovery verification. Claude's OAuth callbacks are FIXED
# (https://claude.ai/api/mcp/auth_callback + claude.com twin) and registered in CloudFormation at
# deploy time, so unlike Codex/ChatGPT there is NO post-deploy callback step. This just verifies the
# discovery chain and prints the setup steps.
#
#   substrate/wire-claude.sh
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

for arg in "$@"; do
  case "${arg}" in
    -h|--help) sed -n '4,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
CLIENT_ID="$(output ClaudeClientId)"

if [[ -z "${MCP_ENDPOINT}" || "${MCP_ENDPOINT}" == "None" || -z "${CLIENT_ID}" || "${CLIENT_ID}" == "None" ]]; then
  echo "ERROR: could not read McpEndpoint/ClaudeClientId from stack '${STACK_NAME}' in ${AWS_REGION}." >&2
  exit 1
fi

echo "==> McpEndpoint:     ${MCP_ENDPOINT}"
echo "==> ClaudeClientId:  ${CLIENT_ID}"
echo ""
echo "==> Verifying the MCP auth discovery chain..."
WWW_AUTH="$(curl -sS -o /dev/null -D - -X POST "${MCP_ENDPOINT}" \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
  | tr -d '\r' | grep -i '^www-authenticate:' || true)"
if [[ "${WWW_AUTH}" == *resource_metadata=* ]]; then
  echo "    [ok] 401 carries WWW-Authenticate resource_metadata"
else
  echo "    [FAIL] MCP endpoint 401 lacks resource_metadata." >&2
  exit 1
fi

cat <<EOF

==> Connect Claude (web or desktop):

    1. claude.ai -> Settings -> Connectors -> Add custom connector:
         URL:        ${MCP_ENDPOINT}
         Client ID:  ${CLIENT_ID}
         (public PKCE client - no client secret; callbacks pre-registered in CloudFormation)
    2. Connect and sign in with your pairputer admin credentials.
    3. In a chat: enable the connector and ask Claude to run play_capsule.

    STATUS: Claude is a stub host — OAuth + tools are expected to work; widget RENDERING is
    best-effort pending PROBE-9 (known upstream bug: modelcontextprotocol/ext-apps#671).
    Record outcomes in docs/hosts/claude.md.
EOF
