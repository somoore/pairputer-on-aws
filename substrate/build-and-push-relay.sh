#!/usr/bin/env bash
#
# Build the ARM64 stateful relay image, push it to ECR, and print a digest-pinned URI.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Standard AWS credential chain (SSO, AWS_PROFILE, ~/.aws/*, env keys, roles).
# Exports AWS_REGION / AWS_DEFAULT_REGION / AWS_ACCOUNT_ID.
# shellcheck source=lib/aws-env.sh
source "${SCRIPT_DIR}/lib/aws-env.sh"
hb_require_aws || exit 1

ECR_REPO="pairputer-stateful-relay"
PLATFORM="linux/arm64"

BUILD_CONTEXT="${SCRIPT_DIR}/stateful-relay"
DOCKERFILE="${BUILD_CONTEXT}/Dockerfile"

REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
REPO_URI="${REGISTRY}/${ECR_REPO}"
TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"

echo "==> Region:        ${AWS_REGION}"
echo "==> Account:       ${AWS_ACCOUNT_ID}"
echo "==> Repository:    ${REPO_URI}"
echo "==> Platform:      ${PLATFORM}"
echo "==> Dockerfile:    ${DOCKERFILE}"
echo "==> Build context: ${BUILD_CONTEXT}"
echo "==> Tags:          latest, ${TIMESTAMP}"

if [[ ! -f "${DOCKERFILE}" ]]; then
  echo "ERROR: Dockerfile not found at ${DOCKERFILE}" >&2
  exit 1
fi

echo "==> Ensuring ECR repository '${ECR_REPO}' exists..."
if aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "    Repository already exists."
else
  aws ecr create-repository \
    --repository-name "${ECR_REPO}" \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability MUTABLE >/dev/null
  echo "    Created."
fi

echo "==> Logging Docker into ECR..."
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

BUILDER="pairputer-builder"
echo "==> Ensuring buildx builder '${BUILDER}' exists..."
if ! docker buildx inspect "${BUILDER}" >/dev/null 2>&1; then
  docker buildx create --name "${BUILDER}" --driver docker-container --bootstrap >/dev/null
  echo "    Created builder."
else
  echo "    Builder already exists."
fi

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
echo "    Digest URI: ${DIGEST_URI}"
echo ""
echo "${DIGEST_URI}"
