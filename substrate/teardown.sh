#!/usr/bin/env bash
#
# teardown.sh — compatibility wrapper around remove-cf.sh.
#
# Kept so existing docs/muscle-memory keep working. remove-cf.sh is the canonical
# teardown (it also lists the nested stacks and can remove the artifact bucket).
#
#   ./teardown.sh                 # delete the stack
#   ./teardown.sh --delete-ecr    # also force-delete regional ECR repos
#   ./teardown.sh --yes           # skip confirmation
#
# For full cleanup including the CFN artifact S3 bucket, use:
#   ./remove-cf.sh --all

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/remove-cf.sh" "$@"
