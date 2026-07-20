#!/usr/bin/env bash
# Re-exec under bash if invoked as `sh <script>` (uses bash-only features). Avoids a cryptic syntax error.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
#
# deploy-capsule.sh — insert a capsule "cartridge" into a deployed pairputer substrate.
#
# Capsules are cartridges (docs/capsule-architecture.md): each is its OWN CloudFormation stack, deployed
# AFTER the substrate. This script packages a capsule's build context, builds the MicroVM image, writes
# immutable manifest + release records to SSM, and atomically advances a per-capsule current pointer only
# after the matching image version exists. The running MCP discovers it by tag at runtime — NO
# substrate/control-plane redeploy. Remove a capsule = delete its stack.
#
# Usage:
#   ./deploy-capsule.sh <capsule-dir-name>          # e.g. agent-doom  (dir under capsules/)
#   ./deploy-capsule.sh agent-doom --name "Agent DOOM" --id agent-doom
#   ./deploy-capsule.sh computer-use-desktop --memory-mib 8192
#   ./deploy-capsule.sh computer-use-desktop --image-name computer-use-desktop-v2 --stack-name pairputer-capsule-computer-use-desktop-v2
#
# Reads capsules/<name>/capsule.yaml for the manifest (if present -> agent-interactive; else Tier 0).
# Requires: aws CLI (deploy creds), the substrate already deployed in this account/region.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/aws-env.sh
source "${SCRIPT_DIR}/lib/aws-env.sh"
hb_require_aws || exit 1

CAPSULE_DIR_NAME="${1:-}"
[[ -n "${CAPSULE_DIR_NAME}" ]] || { echo "usage: deploy-capsule.sh <capsule-dir-name> [--id ID] [--name NAME]" >&2; exit 2; }
[[ "${CAPSULE_DIR_NAME}" =~ ^[a-z0-9][a-z0-9._-]*$ ]] \
  || { echo "ERROR: invalid capsule directory name" >&2; exit 2; }
shift || true

CAPSULE_SOURCE_DIR="${SCRIPT_DIR}/../capsules/${CAPSULE_DIR_NAME}"
[[ -d "${CAPSULE_SOURCE_DIR}" ]] || { echo "ERROR: no capsule dir ${CAPSULE_SOURCE_DIR}" >&2; exit 1; }

# Snapshot code and authority manifest together. The before/after/stage digests reject concurrent
# mutations and symlink/special-file contexts before administrator credentials publish anything.
SOURCE_DIGEST_BEFORE="$(python3 "${SCRIPT_DIR}/tree-digest.py" "${CAPSULE_SOURCE_DIR}")"
STAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pairputer-capsule.XXXXXX")"
trap 'rm -rf "$STAGE_ROOT"' EXIT
CAPSULE_DIR="${STAGE_ROOT}/${CAPSULE_DIR_NAME}"
mkdir -m 0700 "${CAPSULE_DIR}"
cp -a "${CAPSULE_SOURCE_DIR}/." "${CAPSULE_DIR}/"
SOURCE_DIGEST_AFTER="$(python3 "${SCRIPT_DIR}/tree-digest.py" "${CAPSULE_SOURCE_DIR}")"
CONTEXT_DIGEST="$(python3 "${SCRIPT_DIR}/tree-digest.py" "${CAPSULE_DIR}")"
[[ "$SOURCE_DIGEST_BEFORE" == "$SOURCE_DIGEST_AFTER" && "$SOURCE_DIGEST_BEFORE" == "$CONTEXT_DIGEST" ]] \
  || { echo "ERROR: capsule changed while creating the deployment snapshot" >&2; exit 1; }

# Defaults derive from the capsule dir + its manifest; --flags override.
CAPSULE_ID="${CAPSULE_DIR_NAME}"
CAPSULE_NAME=""
CAPSULE_DESC=""
CAPSULE_MIN_MEMORY_MIB="2048"
MANIFEST_JSON=""
MANIFEST_DECLARED_ID=""
if [[ -f "${CAPSULE_DIR}/capsule.yaml" ]]; then
  MANIFEST_JSON="$(python3 "${SCRIPT_DIR}/validate-capsule-manifest.py" "${CAPSULE_DIR}/capsule.yaml")" \
    || { echo "ERROR: capsule manifest failed trusted deployment policy" >&2; exit 1; }
  MANIFEST_DECLARED_ID="$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["capsule"]["id"])' "$MANIFEST_JSON")"
  CAPSULE_ID="$(python3 -c 'import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))["capsule"].get("id",""))' "${CAPSULE_DIR}/capsule.yaml" || echo "${CAPSULE_DIR_NAME}")"
  CAPSULE_NAME="$(python3 -c 'import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))["capsule"].get("name",""))' "${CAPSULE_DIR}/capsule.yaml" || true)"
  CAPSULE_DESC="$(python3 -c 'import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))["capsule"].get("description",""))' "${CAPSULE_DIR}/capsule.yaml" || true)"
  CAPSULE_MIN_MEMORY_MIB="$(python3 -c 'import yaml,sys;c=yaml.safe_load(open(sys.argv[1]))["capsule"];r=c.get("runtime") or {};print(r.get("minimumMemoryMiB",r.get("minMemoryMiB",r.get("memoryMiB",2048))))' "${CAPSULE_DIR}/capsule.yaml" || echo 2048)"
fi
STACK_NAME_OVERRIDE=""
CAPSULE_IMAGE_NAME=""
MEMORY_TIER_OF=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --id) CAPSULE_ID="$2"; shift 2 ;;
    --name) CAPSULE_NAME="$2"; shift 2 ;;
    --description) CAPSULE_DESC="$2"; shift 2 ;;
    --memory-mib) CAPSULE_MIN_MEMORY_MIB="$2"; shift 2 ;;
    --image-name) CAPSULE_IMAGE_NAME="$2"; shift 2 ;;
    --stack-name) STACK_NAME_OVERRIDE="$2"; shift 2 ;;
    # A memory-tier sibling of BASE built from the SAME context (only memory differs). It carries a
    # distinct id/image-name so discovery sees it separately, then folds it into BASE's memoryTiers.
    --memory-tier-of) MEMORY_TIER_OF="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done
# The manifest-id must match the deployment id for a STANDALONE capsule. A memory-tier sibling
# deliberately uses a distinct id (its own image name) while reusing the base's manifest, so the
# match check is skipped when --memory-tier-of is given.
if [[ -z "$MEMORY_TIER_OF" && -n "$MANIFEST_DECLARED_ID" && "$CAPSULE_ID" != "$MANIFEST_DECLARED_ID" ]]; then
  echo "ERROR: deployment id '$CAPSULE_ID' must match manifest id '$MANIFEST_DECLARED_ID'" >&2
  exit 2
fi
[[ "${CAPSULE_MIN_MEMORY_MIB}" =~ ^[0-9]+$ ]] \
  && (( CAPSULE_MIN_MEMORY_MIB >= 2048 && CAPSULE_MIN_MEMORY_MIB <= 32768 )) \
  || { echo "ERROR: memory must be an integer from 2048 through 32768 MiB" >&2; exit 2; }
CAPSULE_IMAGE_NAME="${CAPSULE_IMAGE_NAME:-${CAPSULE_ID}}"
[[ "${CAPSULE_IMAGE_NAME}" =~ ^[a-zA-Z0-9_-]{1,64}$ ]] \
  || { echo "ERROR: image name must be 1..64 letters, digits, underscores, or hyphens" >&2; exit 2; }
CAPSULE_NAME="${CAPSULE_NAME:-${CAPSULE_ID}}"
# Tag values are constrained ([letters/digits/space/_.:/=+-@], <=256). The FULL name/description live in
# the SSM manifest; the tags carry a sanitized copy for discovery display. Em-dashes etc. would 400 the build.
sanitize_tag() { python3 -c 'import re,sys;v=sys.argv[1];v=re.sub(r"[^\w \t.:/=+\-@]","-",v,flags=re.UNICODE);print(v[:256])' "$1"; }
CAPSULE_NAME="$(sanitize_tag "${CAPSULE_NAME}")"
CAPSULE_DESC="$(sanitize_tag "${CAPSULE_DESC}")"
STACK_NAME="${STACK_NAME_OVERRIDE:-pairputer-capsule-${CAPSULE_ID}}"
ARTIFACT_BUCKET="${PAIRPUTER_CFN_BUCKET:-pairputer-cfn-artifacts-${AWS_ACCOUNT_ID}-${AWS_REGION}}"
TEMPLATE="${SCRIPT_DIR}/cloudformation-capsule.yaml"  # published copy; falls back to the repo path below
[[ -f "${TEMPLATE}" ]] || TEMPLATE="${SCRIPT_DIR}/../capsules/nested/capsule-stack.yaml"

echo "==> Capsule:        ${CAPSULE_ID} (${CAPSULE_NAME})"
echo "==> Image name:     ${CAPSULE_IMAGE_NAME}"
echo "==> Stack:          ${STACK_NAME}"
echo "==> Region/Account: ${AWS_REGION} / ${AWS_ACCOUNT_ID}"
echo "==> Runtime memory: ${CAPSULE_MIN_MEMORY_MIB} MiB minimum"

# 1. Package the WAD-free capsule build context to S3 (reuses the substrate's packager + artifact bucket).
create_bucket_if_missing() {
  aws s3api head-bucket --bucket "$1" >/dev/null 2>&1 && return 0
  if [[ "${AWS_REGION}" == "us-east-1" ]]; then aws s3api create-bucket --bucket "$1" --region "${AWS_REGION}" >/dev/null
  else aws s3api create-bucket --bucket "$1" --region "${AWS_REGION}" --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null; fi
}
create_bucket_if_missing "${ARTIFACT_BUCKET}"
echo "==> Packaging capsule context..."
CONTEXT_URI="$(PAIRPUTER_MICROVM_CONTEXT_DIR="${CAPSULE_DIR}" \
  "${SCRIPT_DIR}/package-capsule-image.sh" "${ARTIFACT_BUCKET}" "capsules/${CAPSULE_ID}" | tail -n1)"
[[ -n "${CONTEXT_URI}" ]] || { echo "ERROR: packaging failed" >&2; exit 1; }
echo "    context: ${CONTEXT_URI}"

# 2. Write the capability manifest to SSM (if the capsule has one) so the MCP reads it at runtime.
MANIFEST_SSM=""
MANIFEST_SSM_VALUE=""
MANIFEST_DIGEST=""
CURRENT_RELEASE_SSM=""
if [[ -f "${CAPSULE_DIR}/capsule.yaml" ]]; then
  MANIFEST_JSON="$(python3 -c 'import json,sys;v=json.loads(sys.argv[1]);v["capsule"]["deployment"]={"contextSha256":sys.argv[2],"contextUri":sys.argv[3]};print(json.dumps(v,separators=(",",":"),sort_keys=True))' "$MANIFEST_JSON" "$CONTEXT_DIGEST" "$CONTEXT_URI")"
  MANIFEST_SSM_VALUE="$(python3 -c 'import base64,gzip,sys;raw=sys.argv[1].encode();print("gzip+base64:"+base64.b64encode(gzip.compress(raw,mtime=0)).decode())' "$MANIFEST_JSON")"
  # One SSM parameter caps at 8 KiB. A larger manifest becomes a chunked chain: the primary
  # (digest-addressed) parameter holds a header naming the part count + sha256 of the full payload,
  # and immutable /partN parameters hold the slices. server.py reassembles and verifies the chain.
  MANIFEST_PART_COUNT=0
  MANIFEST_PARTS=()
  if (( ${#MANIFEST_SSM_VALUE} > 8192 )); then
    MANIFEST_FULL_SHA="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$MANIFEST_SSM_VALUE")"
    while IFS= read -r _part; do MANIFEST_PARTS+=("$_part"); done < <(python3 -c '
import sys
v = sys.argv[1]
for i in range(0, len(v), 7500):
    print(v[i:i+7500])' "$MANIFEST_SSM_VALUE")
    MANIFEST_PART_COUNT=${#MANIFEST_PARTS[@]}
    (( MANIFEST_PART_COUNT >= 1 && MANIFEST_PART_COUNT <= 16 )) \
      || { echo "ERROR: capsule manifest needs ${MANIFEST_PART_COUNT} chunks; 16 is the ceiling" >&2; exit 1; }
    MANIFEST_SSM_VALUE="chunked:v1:${MANIFEST_PART_COUNT}:${MANIFEST_FULL_SHA}"
    echo "==> Manifest exceeds one SSM parameter; chunking into ${MANIFEST_PART_COUNT} immutable parts."
  fi
  MANIFEST_DIGEST="sha256:$(python3 -c 'import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$MANIFEST_SSM_VALUE")"
  MANIFEST_SSM="/pairputer/capsules/${CAPSULE_ID}/manifests/sha256-${MANIFEST_DIGEST#sha256:}"
  CURRENT_RELEASE_SSM="/pairputer/capsules/${CAPSULE_ID}/current"
  echo "==> Immutable manifest prepared: ${MANIFEST_DIGEST}"
  echo "    its bytes will be staged now; current remains unchanged until the image stack succeeds."
fi

# Immutable, content-addressed records may safely be staged before the image build: no runtime discovers
# them until CloudFormation's release publisher advances `/current`. Existing bytes must match exactly;
# an impossible digest/path collision or a tampered record fails closed and is never overwritten.
put_immutable_parameter() {
  local name="$1" value="$2" existing=""
  if existing="$(aws ssm get-parameter --name "$name" --region "${AWS_REGION}" --query 'Parameter.Value' --output text 2>/dev/null)"; then
    [[ "$existing" == "$value" ]] \
      || { echo "ERROR: immutable SSM collision at ${name}; refusing overwrite" >&2; return 1; }
    return 0
  fi
  aws ssm put-parameter --name "$name" --type String --tier Intelligent-Tiering \
    --value "$value" --region "${AWS_REGION}" \
    --tags "Key=pairputer:capsule,Value=true" "Key=pairputer:capsule-id,Value=${CAPSULE_ID}" \
           "Key=pairputer:immutable,Value=true" >/dev/null
}
if [[ -n "${MANIFEST_SSM}" ]]; then
  # Parts first, primary last: a reader that can see the header can already fetch every part.
  for ((i = 0; i < MANIFEST_PART_COUNT; i++)); do
    put_immutable_parameter "${MANIFEST_SSM}/part${i}" "${MANIFEST_PARTS[$i]}"
  done
  put_immutable_parameter "${MANIFEST_SSM}" "${MANIFEST_SSM_VALUE}"
  echo "==> Staged immutable manifest ${MANIFEST_SSM} (not current/discoverable yet)."
fi

# 3. Deploy the capsule stack — builds + tags the image for tag-based discovery.
echo "==> Deploying capsule stack (image build is async, several minutes)..."

run_capsule_deploy() {
  aws cloudformation deploy \
    --template-file "${TEMPLATE}" \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" \
    --no-fail-on-empty-changeset \
    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_IAM \
    --parameter-overrides \
      "CapsuleContextUri=${CONTEXT_URI}" \
      "CapsuleContextBucket=${ARTIFACT_BUCKET}" \
      "CapsuleContextSha256=${CONTEXT_DIGEST}" \
      "CapsuleImageName=${CAPSULE_IMAGE_NAME}" \
      "CapsuleId=${CAPSULE_ID}" \
      "CapsuleDisplayName=${CAPSULE_NAME}" \
      "CapsuleDescription=${CAPSULE_DESC}" \
      "CapsuleManifestSsmParam=${MANIFEST_SSM}" \
      "CapsuleManifestDigest=${MANIFEST_DIGEST}" \
      "CapsuleReleaseSsmParam=${CURRENT_RELEASE_SSM}" \
      "CapsuleMinMemoryMiB=${CAPSULE_MIN_MEMORY_MIB}" \
      "MemoryTierOf=${MEMORY_TIER_OF}" \
      "MemoryMib=$([[ -n "$MEMORY_TIER_OF" ]] && echo "$CAPSULE_MIN_MEMORY_MIB" || echo "")" 2>&1
}

# The AWS-managed AWS::Lambda::MicrovmImage resource (CapsuleMicrovmImage) intermittently fails with
# "did not stabilize" on first create. Retry that specific transient flake — a full re-deploy re-polls
# the image and it usually stabilizes on a later pass. A real config error fails identically each time
# and still surfaces. Same logic deploy.sh uses. Null-safe event scan (contains() on a null
# ResourceStatusReason throws and silently disables detection). Tune with PAIRPUTER_DEPLOY_RETRIES.
CAPSULE_DEPLOY_MAX_ATTEMPTS="${PAIRPUTER_DEPLOY_RETRIES:-2}"
capsule_events_have_flake() {  # $1 = stack name/arn; 0 if events mention the image flake
  aws cloudformation describe-stack-events --stack-name "$1" --region "${AWS_REGION}" \
    --query 'StackEvents[].[ResourceStatusReason,LogicalResourceId]' --output text 2>/dev/null \
    | grep -qiE "did not stabilize|CapsuleMicrovmImage|MicrovmImage"
}
cap_attempt=0
while :; do
  set +e
  CAP_OUTPUT="$(run_capsule_deploy)"
  CAP_RC=$?
  set -e
  echo "${CAP_OUTPUT}"
  [[ ${CAP_RC} -eq 0 ]] && break
  echo "${CAP_OUTPUT}" | grep -qiE "No changes to deploy|No updates are to be performed" && break

  CAP_FLAKE="false"
  if echo "${CAP_OUTPUT}" | grep -qiE "did not stabilize|MicrovmImage"; then
    CAP_FLAKE="true"
  elif capsule_events_have_flake "${STACK_NAME}"; then
    CAP_FLAKE="true"
  fi

  if [[ "${CAP_FLAKE}" == "true" && ${cap_attempt} -lt ${CAPSULE_DEPLOY_MAX_ATTEMPTS} ]]; then
    cap_attempt=$((cap_attempt + 1))
    echo "==> Capsule image hit the flaky 'did not stabilize'; retrying (attempt ${cap_attempt}/${CAPSULE_DEPLOY_MAX_ATTEMPTS})..." >&2
    # NOT `wait stack-rollback-complete`: that waiter never matches a CREATE rollback's
    # ROLLBACK_COMPLETE and blind-polls for 60 minutes (see hb_wait_stack_settled in lib/aws-env.sh).
    hb_wait_stack_settled "${STACK_NAME}"
    # A failed FIRST create leaves ROLLBACK_COMPLETE, which can't be updated — delete before retrying.
    CAP_STATE="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
      --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
    if [[ "${CAP_STATE}" == "ROLLBACK_COMPLETE" || "${CAP_STATE}" == "ROLLBACK_FAILED" ]]; then
      echo "    Stack is ${CAP_STATE}; deleting before the clean re-create..." >&2
      aws cloudformation delete-stack --stack-name "${STACK_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1 || true
      aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1 || true
    fi
    continue
  fi

  echo "ERROR: capsule stack deploy failed (exit ${CAP_RC})." >&2
  [[ "${CAP_FLAKE}" == "true" ]] && echo "       The MicroVM image failed to stabilize even after ${CAPSULE_DEPLOY_MAX_ATTEMPTS} retries; re-run to resume." >&2
  exit "${CAP_RC}"
done

# CloudFormation's release publisher runs only after the matching ACTIVE image version exists. It verifies
# the staged manifest digest, creates the immutable release, then advances `/current` as its final write.
# The update is serialized with the image update and rollback restores the previous release pointer.
if [[ -n "${MANIFEST_SSM}" ]]; then
  read -r IMAGE_ARN IMAGE_VERSION RELEASE_DIGEST RELEASE_SSM < <(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
    --query 'Stacks[0].[Outputs[?OutputKey==`CapsuleImageArn`].OutputValue|[0],Outputs[?OutputKey==`CapsuleLatestActiveVersion`].OutputValue|[0],Outputs[?OutputKey==`CapsuleReleaseDigest`].OutputValue|[0],Outputs[?OutputKey==`CapsuleReleaseParameter`].OutputValue|[0]]' \
    --output text)
  for required in IMAGE_ARN IMAGE_VERSION RELEASE_DIGEST RELEASE_SSM; do
    [[ -n "${!required:-}" && "${!required}" != "None" ]] \
      || { echo "ERROR: capsule stack omitted ${required}; release publication did not complete" >&2; exit 1; }
  done
  echo "==> Release committed: ${RELEASE_DIGEST}"
  echo "    image:    ${IMAGE_ARN}:${IMAGE_VERSION}"
  echo "    manifest: ${MANIFEST_SSM}"
  echo "    release:  ${RELEASE_SSM}"
  echo "    current:  ${CURRENT_RELEASE_SSM}"
fi

echo ""
echo "==> Capsule '${CAPSULE_ID}' inserted. The running MCP will discover it by tag within its cache TTL."
echo "    Remove it later:  aws cloudformation delete-stack --stack-name ${STACK_NAME} --region ${AWS_REGION}"
