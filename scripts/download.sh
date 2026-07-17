#!/usr/bin/env bash
#
# pairputer Codex setup bootstrap.
#
# For the 1-click (console) deploy path: fetches wire-codex.sh (+ its one dependency) from the pairputer
# repo into a temp dir and runs it, so you don't have to clone the whole repo. wire-codex.sh reads your
# deployed stack's outputs and:
#   1. writes the [mcp_servers.pairputer] block into ~/.codex/config.toml, and
#   2. registers Codex's OAuth callback with your Cognito app client (so login doesn't fail on a
#      redirect_uri mismatch).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/pairputer/pairputer-platform/main/scripts/download.sh -o pairputer-setup.sh
#   less pairputer-setup.sh          # review it — never pipe a config-editing script straight to a shell
#   AWS_REGION=us-east-1 bash pairputer-setup.sh
#
# Env (all optional; forwarded to wire-codex.sh):
#   AWS_REGION / PAIRPUTER_AWS_REGION   region of your deploy (default us-east-1)
#   PAIRPUTER_STACK_NAME                default pairputer
#   PAIRPUTER_CODEX_SERVER_NAME         default pairputer
#   PAIRPUTER_REF                       repo ref to fetch from (default main)
#   PAIRPUTER_GITHUB_REPO               owner/repo to fetch from (default pairputer/pairputer-platform)
set -euo pipefail

REF="${PAIRPUTER_REF:-main}"
REPO="${PAIRPUTER_GITHUB_REPO:-pairputer/pairputer-platform}"
BASE="https://raw.githubusercontent.com/${REPO}/${REF}/substrate"
: "${AWS_REGION:=${PAIRPUTER_AWS_REGION:-us-east-1}}"; export AWS_REGION

command -v aws >/dev/null || { echo "ERROR: AWS CLI not found. Install it, then re-run."; exit 1; }
command -v curl >/dev/null || { echo "ERROR: curl not found."; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/lib"
echo "==> Fetching pairputer Codex setup scripts (ref: ${REF})..."
curl -fsSL "${BASE}/wire-codex.sh"    -o "${WORK}/wire-codex.sh"
curl -fsSL "${BASE}/lib/aws-env.sh"   -o "${WORK}/lib/aws-env.sh"
chmod +x "${WORK}/wire-codex.sh"

echo "==> Running wire-codex.sh (region: ${AWS_REGION})..."
echo
bash "${WORK}/wire-codex.sh" "$@"
