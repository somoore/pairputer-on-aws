#!/usr/bin/env bash
#
# build-and-push.sh
# -----------------
# Build the ARM64 pairputer MCP server image, push it to ECR, and print the
# fully-qualified image URI (with digest) for use as the CloudFormation
# `ContainerUri` parameter.
#
# AgentCore Runtime REQUIRES a single-arch linux/arm64 image whose manifest is a
# plain image manifest (NOT an OCI image index / "manifest list"). buildx will
# wrap a normal build in a manifest list and attach a provenance attestation,
# which AgentCore + ECR reject for a single-platform runtime image. We therefore
# build with `--provenance=false --sbom=false` and a single `--platform`, which
# makes buildx push a clean `application/vnd.docker.distribution.manifest.v2+json`
# image manifest that ECR + AgentCore accept.
#
# Building arm64 on an arm64 (Apple Silicon) Mac is native — no QEMU emulation.
#
# Usage:
#   ./build-and-push.sh
#
# On success the LAST line of stdout is the digest-pinned image URI, so callers
# (e.g. deploy.sh) can capture it with:
#   IMAGE_URI=$(./build-and-push.sh | tail -n1)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration. Resolve paths relative to this script so it works from any CWD.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Standard AWS credential chain (SSO, AWS_PROFILE, ~/.aws/*, env keys, roles).
# Exports AWS_REGION / AWS_DEFAULT_REGION / AWS_ACCOUNT_ID.
# shellcheck source=lib/aws-env.sh
source "${SCRIPT_DIR}/lib/aws-env.sh"
hb_require_aws || exit 1

ECR_REPO="pairputer-mcp"
PLATFORM="linux/arm64"

BUILD_CONTEXT="${SCRIPT_DIR}/mcp-server"
DOCKERFILE="${BUILD_CONTEXT}/Dockerfile"

REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
REPO_URI="${REGISTRY}/${ECR_REPO}"
TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"   # immutable, sortable tag

echo "==> Region:        ${AWS_REGION}"
echo "==> Account:       ${AWS_ACCOUNT_ID}"
echo "==> Repository:    ${REPO_URI}"
echo "==> Platform:      ${PLATFORM}"
echo "==> Dockerfile:    ${DOCKERFILE}"
echo "==> Build context: ${BUILD_CONTEXT}"
echo "==> Tags:          latest, ${TIMESTAMP}"

# Sanity check: make sure the build inputs actually exist.
if [[ ! -f "${DOCKERFILE}" ]]; then
  echo "ERROR: Dockerfile not found at ${DOCKERFILE}" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. Ensure the ECR repository exists (idempotent: create only if missing).
# ---------------------------------------------------------------------------
echo "==> Ensuring ECR repository '${ECR_REPO}' exists..."
if aws ecr describe-repositories \
      --repository-names "${ECR_REPO}" \
      --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "    Repository already exists."
else
  echo "    Creating repository..."
  aws ecr create-repository \
    --repository-name "${ECR_REPO}" \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability MUTABLE >/dev/null
  echo "    Created."
fi

# ---------------------------------------------------------------------------
# 2. Authenticate Docker to ECR.
# ---------------------------------------------------------------------------
echo "==> Logging Docker into ECR..."
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

# ---------------------------------------------------------------------------
# 3. Ensure a buildx builder exists (buildx is required for --provenance/--platform).
#    Use a dedicated builder so we don't depend on the host's default config.
# ---------------------------------------------------------------------------
BUILDER="pairputer-builder"
echo "==> Ensuring buildx builder '${BUILDER}' exists..."
if ! docker buildx inspect "${BUILDER}" >/dev/null 2>&1; then
  docker buildx create --name "${BUILDER}" --driver docker-container --bootstrap >/dev/null
  echo "    Created builder."
else
  echo "    Builder already exists."
fi

# ---------------------------------------------------------------------------
# 4. Build the single-arch arm64 image and push BOTH tags in one go.
#
#    Key flags (the ECR / AgentCore gotchas):
#      --platform linux/arm64   single arch -> single image manifest (no index).
#      --provenance=false       suppress the provenance attestation that would
#                               otherwise force buildx to emit a manifest LIST.
#      --sbom=false             likewise suppress SBOM attestation.
#      --output type=image,push=true
#                               push the resulting image manifest directly to ECR.
#                               (Equivalent to --push, written explicitly to make
#                               the "type=image" contract obvious.)
# ---------------------------------------------------------------------------
echo "==> Building and pushing ${PLATFORM} image (tags: latest, ${TIMESTAMP})..."
docker buildx build \
  --builder "${BUILDER}" \
  --platform "${PLATFORM}" \
  --provenance=false \
  --sbom=false \
  --file "${DOCKERFILE}" \
  --tag "${REPO_URI}:latest" \
  --tag "${REPO_URI}:${TIMESTAMP}" \
  --output type=image,push=true \
  "${BUILD_CONTEXT}"

# ---------------------------------------------------------------------------
# 5. Resolve the pushed image digest so we can print a digest-pinned URI.
#    Pinning by digest (not just tag) makes the deployed image immutable and
#    unambiguous for AgentCore.
# ---------------------------------------------------------------------------
echo "==> Resolving pushed image digest..."
IMAGE_DIGEST="$(aws ecr describe-images \
  --repository-name "${ECR_REPO}" \
  --region "${AWS_REGION}" \
  --image-ids imageTag="${TIMESTAMP}" \
  --query 'imageDetails[0].imageDigest' \
  --output text)"

DIGEST_URI="${REPO_URI}@${IMAGE_DIGEST}"

echo ""
echo "==> Push complete."
echo "    Tag URI (timestamp): ${REPO_URI}:${TIMESTAMP}"
echo "    Tag URI (latest):    ${REPO_URI}:latest"
echo "    Digest URI:          ${DIGEST_URI}"
echo ""
echo "Pass one of the above as ContainerUri to deploy.sh, e.g.:"
echo "    ./deploy.sh '${DIGEST_URI}'"
echo ""

# IMPORTANT: keep this as the final line of stdout so it can be captured with
# \`tail -n1\`. We emit the digest-pinned URI (most reproducible).
echo "${DIGEST_URI}"
