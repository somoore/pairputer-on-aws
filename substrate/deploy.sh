#!/usr/bin/env bash
# Re-exec under bash if invoked as `sh deploy.sh` (POSIX sh lacks arrays/[[ ]]/process-substitution
# this script uses). Without this, `sh deploy.sh` fails with a cryptic syntax error.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
#
# Deploy the pairputer CloudFormation stack.
#
# Everything defaults to one AWS region: the region resolved from
# PAIRPUTER_AWS_REGION, AWS_REGION, or the AWS CLI profile. No helper stack is
# deployed in a second region. The CloudFront-scope WAF is nested into the root
# stack when the target region is us-east-1, which is the only region AWS allows
# for CLOUDFRONT-scope WAFv2 resources.
#
# Usage:
#   ./deploy.sh [CONTAINER_URI] [RELAY_CONTAINER_URI]
#
# This script packages the WAD-free MicroVM build context, uploads it to S3,
# and CloudFormation creates the DOOM MicroVM image inside the target
# account/region. Supplying a prebuilt external MicroVM image is intentionally
# gated behind PAIRPUTER_ALLOW_EXTERNAL_DOOM_IMAGE=true because it bypasses the
# stack-managed DoomImageStack and can point AgentCore at an image with no
# active version.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/cloudformation/pairputer.yaml"

# Standard AWS credential chain (SSO, AWS_PROFILE, ~/.aws/*, env keys, roles).
# Verifies creds resolve and exports AWS_REGION / AWS_DEFAULT_REGION / AWS_ACCOUNT_ID.
# shellcheck source=lib/aws-env.sh
source "${SCRIPT_DIR}/lib/aws-env.sh"
hb_require_aws || exit 1

STACK_NAME="${PAIRPUTER_STACK_NAME:-pairputer}"
PACKAGED_TEMPLATE="${PAIRPUTER_PACKAGED_TEMPLATE:-/tmp/${STACK_NAME}-${AWS_REGION}-packaged.yaml}"

CONTAINER_URI="${1:-${PAIRPUTER_CONTAINER_URI:-}}"
RELAY_CONTAINER_URI="${2:-${PAIRPUTER_RELAY_CONTAINER_URI:-}}"
DOOM_IMAGE_ARN_OVERRIDE="${3:-${PAIRPUTER_DOOM_IMAGE_ARN_OVERRIDE:-${PAIRPUTER_DOOM_IMAGE_ARN:-}}}"
ALLOW_EXTERNAL_DOOM_IMAGE="${PAIRPUTER_ALLOW_EXTERNAL_DOOM_IMAGE:-false}"

if [[ "${ALLOW_EXTERNAL_DOOM_IMAGE}" != "true" && "${ALLOW_EXTERNAL_DOOM_IMAGE}" != "false" ]]; then
  echo "ERROR: PAIRPUTER_ALLOW_EXTERNAL_DOOM_IMAGE must be true or false." >&2
  exit 1
fi

if [[ "${RELAY_CONTAINER_URI}" == arn:aws:lambda:*:microvm-image:* ]]; then
  if [[ "${ALLOW_EXTERNAL_DOOM_IMAGE}" != "true" ]]; then
    echo "ERROR: second argument looks like a MicroVM image ARN, not the relay container URI." >&2
    echo "       Normal deploys must let CloudFormation build DoomImageStack." >&2
    echo "       To use an external image deliberately, set PAIRPUTER_ALLOW_EXTERNAL_DOOM_IMAGE=true." >&2
    exit 1
  fi
  DOOM_IMAGE_ARN_OVERRIDE="${RELAY_CONTAINER_URI}"
  RELAY_CONTAINER_URI=""
fi

if [[ -n "${DOOM_IMAGE_ARN_OVERRIDE}" && "${ALLOW_EXTERNAL_DOOM_IMAGE}" != "true" ]]; then
  echo "ERROR: DoomImageArnOverride is set but external MicroVM images are not enabled." >&2
  echo "       Omit the third deploy argument and unset PAIRPUTER_DOOM_IMAGE_ARN_OVERRIDE/PAIRPUTER_DOOM_IMAGE_ARN." >&2
  echo "       If this is intentional, rerun with PAIRPUTER_ALLOW_EXTERNAL_DOOM_IMAGE=true." >&2
  exit 1
fi

if [[ ! -f "${TEMPLATE_FILE}" ]]; then
  echo "ERROR: template not found at ${TEMPLATE_FILE}" >&2
  exit 1
fi

ARTIFACT_BUCKET="${PAIRPUTER_CFN_BUCKET:-pairputer-cfn-artifacts-${AWS_ACCOUNT_ID}-${AWS_REGION}}"

create_bucket_if_missing() {
  local bucket="$1"
  if aws s3api head-bucket --bucket "${bucket}" >/dev/null 2>&1; then
    return
  fi
  if [[ "${AWS_REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${bucket}" --region "${AWS_REGION}" >/dev/null
  else
    aws s3api create-bucket \
      --bucket "${bucket}" \
      --region "${AWS_REGION}" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
  fi
  aws s3api put-public-access-block \
    --bucket "${bucket}" \
    --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null
  aws s3api put-bucket-encryption \
    --bucket "${bucket}" \
    --server-side-encryption-configuration \
      '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
}

# run_capture_last: run a helper script and return its last STDOUT line as the result, while
# preserving the helper's real exit code. Using `$(helper | tail)` throws the helper's exit status
# away (tail's status wins), so a failed Docker build silently yields an empty URI and the deploy
# sails on. The build helpers deliberately print only the clean image URI to stdout and send all
# human-facing progress (Docker output, the "Digest URI:" banner, copy-paste hints) to stderr — so
# we capture ONLY stdout for the result and let stderr flow straight to the terminal. Merging them
# (2>&1) would let a stderr hint become the "last line" and poison the URI.
run_capture_last() {
  local out rc
  out="$(mktemp "${TMPDIR:-/tmp}/hb-build.XXXXXX")"
  "$@" >"${out}"            # stdout -> file (the URI); stderr -> terminal (progress)
  rc=$?
  if [[ ${rc} -ne 0 ]]; then
    rm -f "${out}"
    return "${rc}"
  fi
  tail -n1 "${out}"
  rm -f "${out}"
}

if [[ -z "${CONTAINER_URI}" ]]; then
  echo "==> No ContainerUri given; running build-and-push.sh in ${AWS_REGION}..."
  if ! CONTAINER_URI="$(run_capture_last "${SCRIPT_DIR}/build-and-push.sh")" || [[ -z "${CONTAINER_URI}" ]]; then
    echo "ERROR: build-and-push.sh failed (is the Docker daemon running?). Aborting deploy." >&2
    exit 1
  fi
fi

if [[ -z "${RELAY_CONTAINER_URI}" ]]; then
  echo "==> No RelayContainerUri given; running build-and-push-relay.sh in ${AWS_REGION}..."
  if ! RELAY_CONTAINER_URI="$(run_capture_last "${SCRIPT_DIR}/build-and-push-relay.sh")" || [[ -z "${RELAY_CONTAINER_URI}" ]]; then
    echo "ERROR: build-and-push-relay.sh failed (is the Docker daemon running?). Aborting deploy." >&2
    exit 1
  fi
fi

NETWORKING_MODE="${PAIRPUTER_NETWORKING_MODE:-CreateVpcFckNat}"
VPC_ID="${PAIRPUTER_VPC_ID:-}"
PRIVATE_SUBNET_IDS="${PAIRPUTER_PRIVATE_SUBNET_IDS:-}"
VPC_CIDR="${PAIRPUTER_VPC_CIDR:-}"
FCK_NAT_AMI_ID="${PAIRPUTER_FCK_NAT_AMI_ID:-}"
NEW_VPC_CIDR="${PAIRPUTER_NEW_VPC_CIDR:-10.71.0.0/16}"
COGNITO_DOMAIN_PREFIX="${PAIRPUTER_COGNITO_DOMAIN_PREFIX:-pairputer-prod-auth}"
RUNTIME_NAME="${PAIRPUTER_RUNTIME_NAME:-pairputer_mcp_stateful}"
CODEX_CALLBACK_URL="${PAIRPUTER_CODEX_CALLBACK_URL:-}"
SUPER_ADMIN_EMAIL="${PAIRPUTER_SUPER_ADMIN_EMAIL:-${PAIRPUTER_ADMIN_EMAIL:-}}"
RELAY_WARM_SECONDS="${PAIRPUTER_RELAY_WARM_SECONDS:--1}"
# -1 = always-on (default); 0 = scale to zero when idle; N>0 = warm N seconds then down. Scale-to-zero
# is now real + fail-safe (stops only on a strongly-read count of exactly 0 sessions), so 0/N>0 are
# supported. Reject anything that isn't an integer >= -1.
if ! [[ "${RELAY_WARM_SECONDS}" =~ ^-?[0-9]+$ ]] || (( RELAY_WARM_SECONDS < -1 )); then
  echo "ERROR: PAIRPUTER_RELAY_WARM_SECONDS must be an integer >= -1 (-1 always-on, 0 scale-to-zero, N>0 warm-N)." >&2
  exit 1
fi
PAIRPUTER_DEBUG="${PAIRPUTER_DEBUG:-false}"
if [[ "${PAIRPUTER_DEBUG}" != "true" && "${PAIRPUTER_DEBUG}" != "false" ]]; then
  echo "ERROR: PAIRPUTER_DEBUG must be true or false." >&2
  exit 1
fi
INPUT_SELFTEST_ENFORCE="${PAIRPUTER_INPUT_SELFTEST_ENFORCE:-true}"
if [[ "${INPUT_SELFTEST_ENFORCE}" != "true" && "${INPUT_SELFTEST_ENFORCE}" != "false" ]]; then
  echo "ERROR: PAIRPUTER_INPUT_SELFTEST_ENFORCE must be true or false." >&2
  exit 1
fi
# Bundle a reference capsule into the root stack? Default true (batteries-included). Set false to deploy a
# BARE substrate — capsules then arrive as cartridges via deploy-capsule.sh (docs/capsule-architecture.md).
# When false we skip the DOOM image build/reuse/override AND the manifest entirely (the cartridge owns both).
BUNDLE_REFERENCE_CAPSULE="${PAIRPUTER_BUNDLE_REFERENCE_CAPSULE:-true}"
if [[ "${BUNDLE_REFERENCE_CAPSULE}" != "true" && "${BUNDLE_REFERENCE_CAPSULE}" != "false" ]]; then
  echo "ERROR: PAIRPUTER_BUNDLE_REFERENCE_CAPSULE must be true or false." >&2; exit 1
fi
# The built-in cartridge dir under capsules/. agent-doom (Tier 1/2: streams DOOM AND exposes agent tools
# like drive_goal). hellbox-doom is the older Tier 0 stream-only capsule (0 agent tools). One source of
# truth: exported so package-doom-image.sh packages the SAME dir this script reads the manifest from —
# a mismatch is what shipped a toolless Hellbox while the chat said "Agent DOOM".
REFERENCE_CAPSULE="${PAIRPUTER_REFERENCE_CAPSULE:-agent-doom}"
CAPSULE_CONTEXT_DIR="${PAIRPUTER_MICROVM_CONTEXT_DIR:-${SCRIPT_DIR}/../capsules/${REFERENCE_CAPSULE}}"
export PAIRPUTER_MICROVM_CONTEXT_DIR="${CAPSULE_CONTEXT_DIR}"  # package-doom-image.sh reads this
if [[ "${BUNDLE_REFERENCE_CAPSULE}" == "true" && ! -f "${CAPSULE_CONTEXT_DIR}/Dockerfile" ]]; then
  echo "ERROR: reference capsule '${REFERENCE_CAPSULE}' has no Dockerfile at ${CAPSULE_CONTEXT_DIR}." >&2; exit 1
fi
DOOM_IMAGE_NAME="${PAIRPUTER_DOOM_IMAGE_NAME:-${STACK_NAME}-doom}"
DOOM_BASE_IMAGE_ARN="${PAIRPUTER_DOOM_BASE_IMAGE_ARN:-}"
DOOM_BASE_IMAGE_VERSION="${PAIRPUTER_DOOM_BASE_IMAGE_VERSION:-0}"
DOOM_IMAGE_MINIMUM_MEMORY_MIB="${PAIRPUTER_DOOM_IMAGE_MINIMUM_MEMORY_MIB:-2048}"

if [[ "${DOOM_IMAGE_NAME}" =~ [^a-zA-Z0-9_-] || "${#DOOM_IMAGE_NAME}" -gt 64 ]]; then
  echo "ERROR: PAIRPUTER_DOOM_IMAGE_NAME must match ^[a-zA-Z0-9-_]{1,64}$." >&2
  exit 1
fi

if [[ -n "${PAIRPUTER_ADMIN_TEMPORARY_PASSWORD:-}" || -n "${PAIRPUTER_SUPER_ADMIN_TEMPORARY_PASSWORD:-}" ]]; then
  echo "ERROR: password parameters are not supported in CloudFormation." >&2
  echo "       Set PAIRPUTER_SUPER_ADMIN_EMAIL, deploy, then use the stack output command;" >&2
  echo "       it prompts locally and suppresses Cognito's temporary-password email." >&2
  exit 1
fi

if [[ -z "${CODEX_CALLBACK_URL}" ]]; then
  CODEX_CALLBACK_URL="$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" \
    --query "Stacks[0].Parameters[?ParameterKey=='CodexCallbackUrl'].ParameterValue | [0]" \
    --output text 2>/dev/null || true)"
  if [[ -z "${CODEX_CALLBACK_URL}" || "${CODEX_CALLBACK_URL}" == "None" ]]; then
    CODEX_CALLBACK_URL="http://localhost:5555/callback"
  fi
fi

# Relay networking. The internal ALB (required by CloudFront VPC origins) needs PRIVATE subnets.
#   ExistingVpc         -> deployer supplies VpcId + PrivateSubnetIds; we resolve the VPC CIDR.
#   CreateVpcFckNat     -> stack builds a dedicated VPC; we resolve the fck-nat AMI (owner-filtered).
#   CreateVpcNatGateway -> stack builds a dedicated VPC + managed NAT; nothing to resolve.
case "${NETWORKING_MODE}" in
  ExistingVpc)
    if [[ -z "${VPC_ID}" || -z "${PRIVATE_SUBNET_IDS}" ]]; then
      echo "ERROR: NetworkingMode=ExistingVpc needs PAIRPUTER_VPC_ID and PAIRPUTER_PRIVATE_SUBNET_IDS." >&2
      echo "       The subnets MUST be private with a working egress path (NAT / VPC endpoints)." >&2
      exit 1
    fi
    if [[ -z "${VPC_CIDR}" ]]; then
      VPC_CIDR="$(aws ec2 describe-vpcs --region "${AWS_REGION}" --vpc-ids "${VPC_ID}" \
        --query 'Vpcs[0].CidrBlock' --output text 2>/dev/null || true)"
    fi
    if [[ -z "${VPC_CIDR}" || "${VPC_CIDR}" == "None" ]]; then
      echo "ERROR: could not resolve CIDR for ${VPC_ID}; set PAIRPUTER_VPC_CIDR." >&2
      exit 1
    fi
    ;;
  CreateVpcFckNat)
    if [[ -z "${FCK_NAT_AMI_ID}" ]]; then
      # Resolve the latest public fck-nat ARM64 AMI (owner 568608671756) in this region.
      FCK_NAT_AMI_ID="$(aws ec2 describe-images --region "${AWS_REGION}" --owners 568608671756 \
        --filters 'Name=name,Values=fck-nat-al2023-*' 'Name=architecture,Values=arm64' \
        --query 'reverse(sort_by(Images,&CreationDate))[0].ImageId' --output text 2>/dev/null || true)"
    fi
    if [[ -z "${FCK_NAT_AMI_ID}" || "${FCK_NAT_AMI_ID}" == "None" ]]; then
      echo "ERROR: could not resolve a fck-nat AMI in ${AWS_REGION}; set PAIRPUTER_FCK_NAT_AMI_ID," >&2
      echo "       or use PAIRPUTER_NETWORKING_MODE=CreateVpcNatGateway (managed NAT, no AMI needed)." >&2
      exit 1
    fi
    ;;
  CreateVpcNatGateway) ;;
  *)
    echo "ERROR: PAIRPUTER_NETWORKING_MODE must be ExistingVpc, CreateVpcFckNat, or CreateVpcNatGateway." >&2
    exit 1
    ;;
esac

WEB_ACL_ARN="${PAIRPUTER_WEB_ACL_ARN:-}"
ENABLE_CLOUDFRONT_WAF="${PAIRPUTER_ENABLE_CLOUDFRONT_WAF:-${PAIRPUTER_ENABLE_WAF:-true}}"
CLOUDFRONT_WAF_RATE_LIMIT="${PAIRPUTER_CLOUDFRONT_WAF_RATE_LIMIT:-2000000}"
if [[ "${ENABLE_CLOUDFRONT_WAF}" != "true" && "${ENABLE_CLOUDFRONT_WAF}" != "false" ]]; then
  echo "ERROR: PAIRPUTER_ENABLE_CLOUDFRONT_WAF must be true or false." >&2
  exit 1
fi
if [[ ! "${CLOUDFRONT_WAF_RATE_LIMIT}" =~ ^[0-9]+$ || "${CLOUDFRONT_WAF_RATE_LIMIT}" -lt 10000 || "${CLOUDFRONT_WAF_RATE_LIMIT}" -gt 2000000000 ]]; then
  echo "ERROR: PAIRPUTER_CLOUDFRONT_WAF_RATE_LIMIT must be an integer from 10000 to 2000000000." >&2
  exit 1
fi


echo "==> Ensuring CloudFormation artifact bucket ${ARTIFACT_BUCKET} exists in ${AWS_REGION}..."
create_bucket_if_missing "${ARTIFACT_BUCKET}"

# Adopt an existing in-account image with our name instead of trying to create a duplicate.
# AWS::Lambda::MicrovmImage names are account-unique, so if a prior image named ${DOOM_IMAGE_NAME}
# already exists (e.g. remove-cf.sh had to RETAIN it because a leftover MicroVM blocked the delete),
# a fresh deploy that tries to CREATE it fails with "already exists". Detect it (tagging API — no
# MicroVM-specific CLI service needed) and reuse it via the override path. Opt out with
# PAIRPUTER_FORCE_REBUILD_DOOM_IMAGE=true.
#
# BUT only adopt an image that is actually USABLE — a name can linger in the tagging API while the
# image is DELETING or FAILED, and adopting that would fail the deploy just as badly. We confirm the
# state with the JS SDK (present after a relay build). If the SDK is unavailable we adopt on the
# tagging-API signal alone; the template's DoomImageOverrideValidation custom resource is the backstop.
detect_doom_stack_ownership() {
  local stack_name="$1" region="$2" error_file physical_id error_text rc
  error_file="$(mktemp "${TMPDIR:-/tmp}/pairputer-cfn-lookup.XXXXXX")"
  if physical_id="$(aws cloudformation describe-stack-resource \
      --stack-name "${stack_name}" --logical-resource-id DoomImageStack \
      --region "${region}" --query 'StackResourceDetail.PhysicalResourceId' --output text \
      2>"${error_file}")"; then
    rm -f "${error_file}"
    if [[ -z "${physical_id}" || "${physical_id}" == "None" ]]; then
      echo "ERROR: CloudFormation returned no physical ID for DoomImageStack; refusing to infer ownership." >&2
      return 1
    fi
    printf '%s\n' "managed"
    return 0
  else
    rc=$?
  fi

  error_text="$(<"${error_file}")"
  rm -f "${error_file}"
  # DescribeStackResource uses ValidationError for both an absent stack and an absent logical resource.
  # Only those explicit not-found messages mean "not managed". Access, credential, throttling, network,
  # malformed-request, and all unknown failures abort the deploy rather than silently adopting an image.
  if [[ "${error_text}" == *"(ValidationError)"* ]]; then
    local lower_error
    lower_error="$(printf '%s' "${error_text}" | tr '[:upper:]' '[:lower:]')"
    if [[ "${lower_error}" == *"stack with id "*" does not exist"* ||
          "${lower_error}" == *"resource "*" does not exist for stack "* ||
          "${lower_error}" == *"logical resource id "*" doesn't exist in stack "* ]]; then
      printf '%s\n' "absent"
      return 0
    fi
  fi
  echo "ERROR: unable to determine whether ${stack_name} owns DoomImageStack; refusing to continue." >&2
  [[ -n "${error_text}" ]] && echo "${error_text}" >&2
  return "${rc}"
}

STACK_MANAGES_DOOM_IMAGE="false"
if [[ "${BUNDLE_REFERENCE_CAPSULE}" == "true" ]]; then
  DOOM_STACK_OWNERSHIP="$(detect_doom_stack_ownership "${STACK_NAME}" "${AWS_REGION}")" || exit $?
  if [[ "${DOOM_STACK_OWNERSHIP}" == "managed" ]]; then
    STACK_MANAGES_DOOM_IMAGE="true"
  elif [[ "${DOOM_STACK_OWNERSHIP}" != "absent" ]]; then
    echo "ERROR: unexpected DoomImageStack ownership result '${DOOM_STACK_OWNERSHIP}'." >&2
    exit 1
  fi
fi

if [[ "${BUNDLE_REFERENCE_CAPSULE}" != "true" ]]; then
  echo "==> Bare substrate (BundleReferenceCapsule=false): skipping DOOM image build/reuse/override."
elif [[ "${STACK_MANAGES_DOOM_IMAGE}" == "true" ]]; then
  # Preserve root-stack ownership across redeploys. Treating this managed image as an external
  # override would delete DoomImageStack now and force an unnecessary full rebuild next time.
  echo "==> Existing stack-managed DOOM image detected; preserving DoomImageStack ownership."
elif [[ -z "${DOOM_IMAGE_ARN_OVERRIDE}" && "${PAIRPUTER_FORCE_REBUILD_DOOM_IMAGE:-false}" != "true" ]]; then
  EXISTING_DOOM_IMAGE_ARN="$(aws resourcegroupstaggingapi get-resources \
    --region "${AWS_REGION}" --resource-type-filters lambda \
    --query "ResourceTagMappingList[?ends_with(ResourceARN, ':microvm-image:${DOOM_IMAGE_NAME}')].ResourceARN | [0]" \
    --output text 2>/dev/null || true)"
  if [[ -n "${EXISTING_DOOM_IMAGE_ARN}" && "${EXISTING_DOOM_IMAGE_ARN}" != "None" ]]; then
    MVM_SDK="${SCRIPT_DIR}/stateful-relay/node_modules/@aws-sdk/client-lambda-microvms"
    IMAGE_USABLE="unknown"
    if command -v node >/dev/null 2>&1 && [[ -d "${MVM_SDK}" ]]; then
      # Prints "USABLE", "UNUSABLE:<state>", or "GONE"; usable = has an active version and isn't deleting.
      IMAGE_USABLE="$(AWS_REGION="${AWS_REGION}" HB_ARN="${EXISTING_DOOM_IMAGE_ARN}" HB_SDK="${MVM_SDK}" node -e '
        const m=require(process.env.HB_SDK);
        const c=new m.LambdaMicrovmsClient({region:process.env.AWS_REGION});
        c.send(new m.GetMicrovmImageCommand({imageIdentifier:process.env.HB_ARN}))
          .then(g=>{const bad=["DELETING","DELETED","FAILED"];
            if(bad.includes(g.state)||!g.latestActiveImageVersion){console.log("UNUSABLE:"+(g.state||"noversion"));}
            else console.log("USABLE");})
          .catch(e=>console.log(e.name==="ResourceNotFoundException"?"GONE":"UNUSABLE:"+e.name));
      ' 2>/dev/null || echo "unknown")"
    fi
    if [[ "${IMAGE_USABLE}" == USABLE || "${IMAGE_USABLE}" == unknown ]]; then
      echo "==> Found existing MicroVM image '${DOOM_IMAGE_NAME}'; reusing it instead of rebuilding."
      echo "    ${EXISTING_DOOM_IMAGE_ARN}$( [[ "${IMAGE_USABLE}" == unknown ]] && echo '  (state unverified — no local SDK; validator will confirm)')"
      echo "    (set PAIRPUTER_FORCE_REBUILD_DOOM_IMAGE=true to build fresh instead)"
      DOOM_IMAGE_ARN_OVERRIDE="${EXISTING_DOOM_IMAGE_ARN}"
      ALLOW_EXTERNAL_DOOM_IMAGE="true"   # our OWN account's image, adopted deliberately
    else
      echo "==> Existing image '${DOOM_IMAGE_NAME}' is not usable (${IMAGE_USABLE}); building a fresh one." >&2
    fi
  fi
fi

DOOM_CODE_ARTIFACT_URI=""
DOOM_CODE_ARTIFACT_BUCKET=""
if [[ "${BUNDLE_REFERENCE_CAPSULE}" != "true" ]]; then
  :  # bare substrate: no context to package (the cartridge builds its own image)
elif [[ -z "${DOOM_IMAGE_ARN_OVERRIDE}" ]]; then
  echo "==> Packaging WAD-free DOOM MicroVM context..."
  if ! DOOM_CODE_ARTIFACT_URI="$(run_capture_last "${SCRIPT_DIR}/package-doom-image.sh" "${ARTIFACT_BUCKET}" "${STACK_NAME}/microvm-image")" || [[ -z "${DOOM_CODE_ARTIFACT_URI}" ]]; then
    echo "ERROR: package-doom-image.sh failed. Aborting deploy." >&2
    exit 1
  fi
  DOOM_CODE_ARTIFACT_BUCKET="${ARTIFACT_BUCKET}"
else
  echo "==> Using prebuilt DOOM image override; skipping MicroVM image packaging."
fi

echo "==> Packaging nested CloudFormation templates..."
aws cloudformation package \
  --template-file "${TEMPLATE_FILE}" \
  --s3-bucket "${ARTIFACT_BUCKET}" \
  --s3-prefix "${STACK_NAME}/templates" \
  --region "${AWS_REGION}" \
  --output-template-file "${PACKAGED_TEMPLATE}" >/dev/null

echo "==> Stack:            ${STACK_NAME}"
echo "==> Region:           ${AWS_REGION}"
echo "==> Template:         ${PACKAGED_TEMPLATE}"
echo "==> ContainerUri:     ${CONTAINER_URI}"
echo "==> RelayUri:         ${RELAY_CONTAINER_URI}"
echo "==> DoomImageOverride:${DOOM_IMAGE_ARN_OVERRIDE:-<none>}"
echo "==> DoomContextUri:   ${DOOM_CODE_ARTIFACT_URI:-<not needed>}"
echo "==> DoomImageName:    ${DOOM_IMAGE_NAME}"
echo "==> DoomBaseVersion:  ${DOOM_BASE_IMAGE_VERSION}"
echo "==> RuntimeName:      ${RUNTIME_NAME}"
echo "==> NetworkingMode:   ${NETWORKING_MODE}"
if [[ "${NETWORKING_MODE}" == "ExistingVpc" ]]; then
  echo "==> VpcId:            ${VPC_ID} (${VPC_CIDR})"
  echo "==> PrivateSubnets:   ${PRIVATE_SUBNET_IDS}"
else
  echo "==> NewVpcCidr:       ${NEW_VPC_CIDR}"
  [[ "${NETWORKING_MODE}" == "CreateVpcFckNat" ]] && echo "==> fck-nat AMI:      ${FCK_NAT_AMI_ID}"
fi
if [[ -n "${WEB_ACL_ARN}" ]]; then
  echo "==> CloudFront WAF:   provided (${WEB_ACL_ARN})"
elif [[ "${ENABLE_CLOUDFRONT_WAF}" == "true" && "${AWS_REGION}" == "us-east-1" ]]; then
  echo "==> CloudFront WAF:   nested (rate limit ${CLOUDFRONT_WAF_RATE_LIMIT}/5m)"
elif [[ "${ENABLE_CLOUDFRONT_WAF}" == "true" ]]; then
  echo "==> CloudFront WAF:   disabled for ${AWS_REGION}; CLOUDFRONT-scope WAFv2 is us-east-1 only"
else
  echo "==> CloudFront WAF:   disabled by PAIRPUTER_ENABLE_CLOUDFRONT_WAF=false"
fi
echo "==> CognitoDomain:    ${COGNITO_DOMAIN_PREFIX}"
echo "==> CodexCallback:    ${CODEX_CALLBACK_URL}"
echo "==> SuperAdmin:       ${SUPER_ADMIN_EMAIL:-<not configured>}"
echo "==> RelayWarmSec:     ${RELAY_WARM_SECONDS}"
echo "==> PairputerDebug:     ${PAIRPUTER_DEBUG}"
echo "==> SelftestEnforce:  ${INPUT_SELFTEST_ENFORCE}"

PARAMETER_OVERRIDES=(
  # deploy.sh builds + pushes images to the deployer's PRIVATE ECR, so it deploys in Private image mode
  # and hands them in as the Private* params. (Public mode = the signed public-ECR defaults, used by the
  # 1-click console path, which doesn't run this script.)
  "ImageSource=Private"
  "BundleReferenceCapsule=${BUNDLE_REFERENCE_CAPSULE}"
  "PrivateMcpContainerUri=${CONTAINER_URI}"
  "PrivateRelayContainerUri=${RELAY_CONTAINER_URI}"
  "DoomImageArnOverride=${DOOM_IMAGE_ARN_OVERRIDE}"
  "AllowDoomImageArnOverride=${ALLOW_EXTERNAL_DOOM_IMAGE}"
  "DoomCodeArtifactUri=${DOOM_CODE_ARTIFACT_URI}"
  "DoomCodeArtifactBucket=${DOOM_CODE_ARTIFACT_BUCKET}"
  "DoomImageName=${DOOM_IMAGE_NAME}"
  "DoomBaseImageArn=${DOOM_BASE_IMAGE_ARN}"
  "DoomBaseImageVersion=${DOOM_BASE_IMAGE_VERSION}"
  "DoomImageMinimumMemoryMiB=${DOOM_IMAGE_MINIMUM_MEMORY_MIB}"
  "RuntimeName=${RUNTIME_NAME}"
  "CodexCallbackUrl=${CODEX_CALLBACK_URL}"
  "SuperAdminEmail=${SUPER_ADMIN_EMAIL}"
  "NetworkingMode=${NETWORKING_MODE}"
  "VpcId=${VPC_ID}"
  "PrivateSubnetIds=${PRIVATE_SUBNET_IDS}"
  "VpcCidr=${VPC_CIDR}"
  "FckNatAmiId=${FCK_NAT_AMI_ID}"
  "NewVpcCidr=${NEW_VPC_CIDR}"
  "CognitoDomainPrefix=${COGNITO_DOMAIN_PREFIX}"
  "WebAclArn=${WEB_ACL_ARN}"
  "EnableCloudFrontWaf=${ENABLE_CLOUDFRONT_WAF}"
  "CloudFrontWafRateLimitPerFiveMinutes=${CLOUDFRONT_WAF_RATE_LIMIT}"
  "RelayWarmSeconds=${RELAY_WARM_SECONDS}"
  "PairputerDebug=${PAIRPUTER_DEBUG}"
  "InputSelftestEnforce=${INPUT_SELFTEST_ENFORCE}"
)

# Capability manifest: if the packaged capsule context ships a capsule.yaml (an agent-interactive
# capsule like capsules/agent-doom), convert it to JSON and pass it through so the MCP server
# registers the agent tools it declares (interaction.md Tier 1/2). No capsule.yaml = Tier 0 deploy:
# the parameter stays empty and no agent tools exist. Override with PAIRPUTER_CAPSULE_MANIFEST_JSON.
# CAPSULE_CONTEXT_DIR is set above (from REFERENCE_CAPSULE) so the manifest and the packaged image agree.
CAPSULE_MANIFEST_JSON="${PAIRPUTER_CAPSULE_MANIFEST_JSON:-}"
# A bare substrate carries no bundled manifest — discovered cartridges supply their own via SSM.
if [[ "${BUNDLE_REFERENCE_CAPSULE}" != "true" ]]; then CAPSULE_MANIFEST_JSON=""; fi
if [[ "${BUNDLE_REFERENCE_CAPSULE}" == "true" && -z "${CAPSULE_MANIFEST_JSON}" && -f "${CAPSULE_CONTEXT_DIR}/capsule.yaml" ]]; then
  CAPSULE_MANIFEST_JSON="$(python3 -c '
import json, sys
try:
    import yaml
except ImportError:
    sys.stderr.write("WARNING: pyyaml unavailable; capsule.yaml ignored (deploying Tier 0)\n")
    sys.exit(0)
doc = yaml.safe_load(open(sys.argv[1])) or {}
# AgentCore caps the total environment-variable payload at 4000 bytes.  The
# full capsule document is retained in the immutable SSM release; the bundled
# seed only needs the runtime contract (identity, interaction, bridge, tools,
# IAM and safety).  Dropping prose/UX-only fields here keeps the authenticated
# tool catalog intact without silently falling back to Tier 0.
capsule = dict(doc.get("capsule") or doc)
runtime_keys = ("id", "name", "interaction", "bridge", "tools", "permissions", "safety")
runtime = {k: capsule[k] for k in runtime_keys if k in capsule}
print(json.dumps({"capsule": runtime}, separators=(",", ":")))
' "${CAPSULE_CONTEXT_DIR}/capsule.yaml")" || CAPSULE_MANIFEST_JSON=""
  [[ -n "${CAPSULE_MANIFEST_JSON}" ]] && echo "==> Capability manifest: ${CAPSULE_CONTEXT_DIR}/capsule.yaml (agent tools ENABLED)"
fi
# CloudFormation String parameter values are capped at 4096 bytes. Preserve the complete
# manifest (rather than silently dropping the typed tool catalog) using the server's bounded
# gzip+base64 encoding whenever the compact JSON would exceed that transport limit.
if [[ "${#CAPSULE_MANIFEST_JSON}" -gt 3500 && "${CAPSULE_MANIFEST_JSON}" != gzip+base64:* ]]; then
  CAPSULE_MANIFEST_JSON="gzip+base64:$(printf '%s' "${CAPSULE_MANIFEST_JSON}" | python3 -c 'import base64,gzip,sys; print(base64.b64encode(gzip.compress(sys.stdin.buffer.read(),mtime=0)).decode())')"
  if [[ "${#CAPSULE_MANIFEST_JSON}" -gt 4096 ]]; then
    echo "ERROR: compressed capsule manifest is still larger than CloudFormation's 4096-byte parameter limit." >&2
    exit 1
  fi
  echo "==> Capability manifest compressed for CloudFormation (${#CAPSULE_MANIFEST_JSON} bytes)"
fi
PARAMETER_OVERRIDES+=("CapsuleManifestJson=${CAPSULE_MANIFEST_JSON}")
# Durable per-tenant workspace storage (see docs/persistent-workspace.md). Defaults ON, backed by
# the CFN artifacts bucket (the documented prototype store — tenant-storage/<tenantId>/ prefix,
# IAM-scoped to that prefix only). Export PAIRPUTER_TENANT_STORAGE_BUCKET="" to disable explicitly,
# or set a dedicated bucket. The old `:-` default silently WIPED the wiring on any deploy run
# without the env var exported (production drift found 2026-07-13: persistentRestore.enabled=false
# while tenant objects sat in S3) — `-` keeps "unset" and "deliberately empty" distinct.
PARAMETER_OVERRIDES+=("TenantStorageBucket=${PAIRPUTER_TENANT_STORAGE_BUCKET-${ARTIFACT_BUCKET}}")

# Registry id/name/description for the bundled capsule (root stack seeds these into PAIRPUTER_IMAGE_REGISTRY).
# Derived from capsule.yaml so the registry matches the bundled capsule instead of the hardcoded Hellbox.
# Fields are ASCII-sanitized (drop quotes; non-ASCII like em-dash -> '-') since they land inside a JSON
# string built by a CloudFormation !Sub. Tab-delimited so spaces in name/description survive the read.
if [[ "${BUNDLE_REFERENCE_CAPSULE}" == "true" && -f "${CAPSULE_CONTEXT_DIR}/capsule.yaml" ]]; then
  IFS=$'\t' read -r REF_ID REF_NAME REF_DESC < <(python3 -c '
import sys, yaml, re
c = yaml.safe_load(open(sys.argv[1])).get("capsule", {})
def clean(v, fb):
    s = re.sub(r"\s+", " ", str(v or "")).replace(chr(34), "")
    s = s.encode("ascii", "replace").decode().replace("?", "-").strip()
    return s or fb
print(clean(c.get("id"), "agent-doom"), clean(c.get("name"), "Agent DOOM"),
      clean(c.get("description"), "Agent DOOM capsule"), sep="\t")
' "${CAPSULE_CONTEXT_DIR}/capsule.yaml")
  PARAMETER_OVERRIDES+=("ReferenceCapsuleId=${REF_ID}" "ReferenceCapsuleName=${REF_NAME}" "ReferenceCapsuleDescription=${REF_DESC}")
  echo "==> Reference capsule: ${REF_ID} (${REF_NAME})"
fi

echo "==> Deploying stack (this can take several minutes; MicroVM image builds are async)..."

run_deploy() {
  aws cloudformation deploy \
    --template-file "${PACKAGED_TEMPLATE}" \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" \
    --parameter-overrides "${PARAMETER_OVERRIDES[@]}" \
    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --no-fail-on-empty-changeset 2>&1
}

# The AWS-managed AWS::Lambda::MicrovmImage resource is intermittently slow and can fail a
# CloudFormation update with "did not stabilize" / a transient service exception; a full-stack
# update makes CloudFormation re-poll it. In practice it succeeds on a second pass, so retry ONCE
# but only for that specific transient image signature — never mask a real config error.
DEPLOY_MAX_ATTEMPTS="${PAIRPUTER_DEPLOY_RETRIES:-1}"   # extra attempts after the first
attempt=0
while :; do
  set +e
  DEPLOY_OUTPUT="$(run_deploy)"
  DEPLOY_RC=$?
  set -e
  echo "${DEPLOY_OUTPUT}"

  if [[ ${DEPLOY_RC} -eq 0 ]]; then
    echo "==> Deploy complete."
    break
  fi
  if echo "${DEPLOY_OUTPUT}" | grep -qiE "No changes to deploy|No updates are to be performed"; then
    echo "==> No updates to perform; stack already up to date."
    break
  fi

  # Was this the flaky MicrovmImage, or a real failure?
  IMAGE_FLAKE="false"
  if echo "${DEPLOY_OUTPUT}" | grep -qiE "did not stabilize|MicrovmImage"; then
    IMAGE_FLAKE="true"
  else
    # deploy output doesn't always echo the resource reason; check stack events too.
    if aws cloudformation describe-stack-events --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
        --query "StackEvents[?contains(ResourceStatusReason,'did not stabilize') || contains(LogicalResourceId,'Microvm') || contains(LogicalResourceId,'DoomImage')].ResourceStatusReason" \
        --output text 2>/dev/null | grep -qiE "stabilize|Microvm|DoomImage"; then
      IMAGE_FLAKE="true"
    fi
  fi

  if [[ "${IMAGE_FLAKE}" == "true" && ${attempt} -lt ${DEPLOY_MAX_ATTEMPTS} ]]; then
    attempt=$((attempt + 1))
    echo "==> Deploy hit the flaky MicroVM image resource; retrying (attempt ${attempt}/${DEPLOY_MAX_ATTEMPTS})..." >&2
    echo "    (the AWS::Lambda::MicrovmImage resource is slow/flaky; a second pass usually stabilizes it)" >&2
    # If the stack is mid-rollback, let it settle before re-deploying.
    aws cloudformation wait stack-rollback-complete --stack-name "${STACK_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1 || true
    aws cloudformation wait stack-update-complete   --stack-name "${STACK_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1 || true
    continue
  fi

  echo "ERROR: deploy failed (exit ${DEPLOY_RC})." >&2
  if [[ "${IMAGE_FLAKE}" == "true" ]]; then
    echo "       The MicroVM image resource failed to stabilize even after ${DEPLOY_MAX_ATTEMPTS} retr$([[ ${DEPLOY_MAX_ATTEMPTS} -eq 1 ]] && echo y || echo ies)." >&2
    echo "       Re-run substrate/deploy.sh (it resumes), or raise PAIRPUTER_DEPLOY_RETRIES." >&2
  fi
  exit ${DEPLOY_RC}
done

echo ""
echo "==> Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${AWS_REGION}" \
  --query 'Stacks[0].Outputs[].{Key:OutputKey,Value:OutputValue,Description:Description}' \
  --output table

# Create the super-admin user. Two paths:
#   Default (email invite): Cognito generates a one-time temp password and emails it to SuperAdminEmail
#     (COGNITO_DEFAULT sender); the account is FORCE_CHANGE_PASSWORD so the admin sets their own at first
#     login. No password chosen by the operator or stored in CloudFormation.
#   PAIRPUTER_ADMIN_PASSWORD_PROMPT=1: prompt locally (hidden) for a password and set it permanent via
#     admin-set-user-password — no email dependency. Use this when COGNITO_DEFAULT email is unreliable
#     (it is best-effort and rate-limited ~50/day — repeated deploys or strict mail servers can drop the
#     invite silently). The password is typed locally, never passed to CloudFormation.
#   PAIRPUTER_ADMIN_PASSWORD_AUTO=1: HEADLESS/CI — the script generates a strong random password (via
#     AWS get-random-password), sets it permanent, and stores it in Secrets Manager at
#     pairputer/super-admin/<email>. No prompt, no email, no value chosen/typed by a human, nothing in
#     CloudFormation. Retrieve it with `aws secretsmanager get-secret-value --secret-id ...`.
# Idempotent: skips creation if the user exists. Opt out entirely with PAIRPUTER_SKIP_ADMIN_CREATE=1.
if [[ "${PAIRPUTER_SKIP_ADMIN_CREATE:-0}" != "1" && -n "${SUPER_ADMIN_EMAIL}" ]]; then
  USER_POOL_ID="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue | [0]" --output text 2>/dev/null || true)"
  if [[ -z "${USER_POOL_ID}" || "${USER_POOL_ID}" == "None" ]]; then
    echo ""
    echo "==> Could not resolve UserPoolId output; skipping admin creation." >&2
    echo "    Use the CreateSuperAdmin* commands in the stack outputs above." >&2
  else
    echo ""
    ADMIN_EXISTS="false"
    if aws cognito-idp admin-get-user --user-pool-id "${USER_POOL_ID}" \
         --username "${SUPER_ADMIN_EMAIL}" --region "${AWS_REGION}" >/dev/null 2>&1; then
      ADMIN_EXISTS="true"
      echo "==> Super-admin '${SUPER_ADMIN_EMAIL}' already exists; not recreating."
    fi

    if [[ "${PAIRPUTER_ADMIN_PASSWORD_AUTO:-0}" == "1" ]]; then
      # Headless path: the SCRIPT generates a strong random password, sets it on the user, and stores
      # it in AWS Secrets Manager (this account) so it's never chosen/typed by a human and never
      # printed. Retrieve it later with:
      #   aws secretsmanager get-secret-value --secret-id pairputer/super-admin/<email> --query SecretString --output text
      # Not passed to CloudFormation (that path is intentionally rejected above). For unattended/CI
      # deploys where a working login is needed immediately without an email round-trip.
      if [[ "${ADMIN_EXISTS}" != "true" ]]; then
        echo "==> Creating super-admin '${SUPER_ADMIN_EMAIL}' (auto password -> Secrets Manager, no email)..."
        aws cognito-idp admin-create-user --user-pool-id "${USER_POOL_ID}" \
          --username "${SUPER_ADMIN_EMAIL}" \
          --user-attributes "Name=email,Value=${SUPER_ADMIN_EMAIL}" "Name=email_verified,Value=true" \
          --message-action SUPPRESS \
          --region "${AWS_REGION}" >/dev/null 2>&1 || true
        aws cognito-idp admin-add-user-to-group --user-pool-id "${USER_POOL_ID}" \
          --username "${SUPER_ADMIN_EMAIL}" --group-name SuperAdmins --region "${AWS_REGION}" >/dev/null 2>&1 || true
      fi
      # Generate a policy-satisfying password (>=16 chars, upper/lower/number/symbol) via AWS, so the
      # value is never authored locally. --require-each-included-type guarantees the Cognito policy.
      HB_ADMIN_PW="$(aws secretsmanager get-random-password --region "${AWS_REGION}" \
        --password-length 24 --require-each-included-type --exclude-punctuation \
        --query RandomPassword --output text 2>/dev/null)$(printf '!Aa1')"
      HB_ADMIN_SECRET_ID="pairputer/super-admin/${SUPER_ADMIN_EMAIL}"
      if [[ -n "${HB_ADMIN_PW}" ]]; then
        if aws cognito-idp admin-set-user-password --user-pool-id "${USER_POOL_ID}" \
             --username "${SUPER_ADMIN_EMAIL}" --password "${HB_ADMIN_PW}" --permanent \
             --region "${AWS_REGION}" >/dev/null 2>&1; then
          # Store (or rotate) the secret so the owner can retrieve it; never echoed to the terminal.
          if aws secretsmanager describe-secret --secret-id "${HB_ADMIN_SECRET_ID}" --region "${AWS_REGION}" >/dev/null 2>&1; then
            aws secretsmanager put-secret-value --secret-id "${HB_ADMIN_SECRET_ID}" \
              --secret-string "${HB_ADMIN_PW}" --region "${AWS_REGION}" >/dev/null 2>&1 || true
          else
            aws secretsmanager create-secret --name "${HB_ADMIN_SECRET_ID}" \
              --description "pairputer super-admin password for ${SUPER_ADMIN_EMAIL} (auto-generated headless)" \
              --secret-string "${HB_ADMIN_PW}" --region "${AWS_REGION}" >/dev/null 2>&1 || true
          fi
          echo "    Password set + stored in Secrets Manager: ${HB_ADMIN_SECRET_ID}"
          echo "    Retrieve: aws secretsmanager get-secret-value --secret-id ${HB_ADMIN_SECRET_ID} --query SecretString --output text"
        else
          echo "    Could not set the auto password (check the pool password policy)." >&2
        fi
        unset HB_ADMIN_PW
      fi
    elif [[ "${PAIRPUTER_ADMIN_PASSWORD_PROMPT:-0}" == "1" ]]; then
      # Local-password path: create WITHOUT an email invite, then set a permanent password typed here.
      if [[ "${ADMIN_EXISTS}" != "true" ]]; then
        echo "==> Creating super-admin '${SUPER_ADMIN_EMAIL}' (local password, no email)..."
        aws cognito-idp admin-create-user --user-pool-id "${USER_POOL_ID}" \
          --username "${SUPER_ADMIN_EMAIL}" \
          --user-attributes "Name=email,Value=${SUPER_ADMIN_EMAIL}" "Name=email_verified,Value=true" \
          --message-action SUPPRESS \
          --region "${AWS_REGION}" >/dev/null 2>&1 || true
        aws cognito-idp admin-add-user-to-group --user-pool-id "${USER_POOL_ID}" \
          --username "${SUPER_ADMIN_EMAIL}" --group-name SuperAdmins --region "${AWS_REGION}" >/dev/null 2>&1 || true
      fi
      read -rsp "    Set super-admin password for ${SUPER_ADMIN_EMAIL}: " HB_ADMIN_PW; echo
      if [[ -n "${HB_ADMIN_PW}" ]]; then
        if aws cognito-idp admin-set-user-password --user-pool-id "${USER_POOL_ID}" \
             --username "${SUPER_ADMIN_EMAIL}" --password "${HB_ADMIN_PW}" --permanent \
             --region "${AWS_REGION}" >/dev/null 2>&1; then
          echo "    Password set. Log in with it via: codex mcp login pairputer"
        else
          echo "    Could not set the password (check the pool password policy: min 12, upper/lower/number)." >&2
        fi
        unset HB_ADMIN_PW
      else
        echo "    No password entered; skipped. Set one later via SetSuperAdminPermanentPasswordCommand." >&2
      fi
    elif [[ "${ADMIN_EXISTS}" != "true" ]]; then
      # Default email-invite path.
      echo "==> Creating super-admin '${SUPER_ADMIN_EMAIL}' (Cognito will email a temporary password)..."
      if aws cognito-idp admin-create-user --user-pool-id "${USER_POOL_ID}" \
           --username "${SUPER_ADMIN_EMAIL}" \
           --user-attributes "Name=email,Value=${SUPER_ADMIN_EMAIL}" "Name=email_verified,Value=true" \
           --desired-delivery-mediums EMAIL \
           --region "${AWS_REGION}" >/dev/null 2>&1; then
        aws cognito-idp admin-add-user-to-group --user-pool-id "${USER_POOL_ID}" \
          --username "${SUPER_ADMIN_EMAIL}" --group-name SuperAdmins --region "${AWS_REGION}" >/dev/null 2>&1 || true
        echo "    Done. Check ${SUPER_ADMIN_EMAIL} for the temporary password (set a permanent one at"
        echo "    first login). Cognito's default email is best-effort/rate-limited (~50/day); if it"
        echo "    doesn't arrive, re-run with PAIRPUTER_ADMIN_PASSWORD_PROMPT=1 to set a password directly."
      else
        echo "    admin-create-user failed; create the user with a stack-output command instead." >&2
      fi
    fi
  fi
fi

echo ""
# Wire the local Codex config automatically (upsert url + client_id, back up first),
# then print the interactive login command. Opt out with PAIRPUTER_SKIP_CODEX_CONFIG=1
# (e.g. deploying to an account that is not your Codex target, or in CI).
if [[ "${PAIRPUTER_SKIP_CODEX_CONFIG:-0}" == "1" ]]; then
  echo "==> Skipping Codex config wiring (PAIRPUTER_SKIP_CODEX_CONFIG=1)."
  echo "==> Done. Use McpEndpoint directly for the Codex 'url' field."
else
  echo "==> Wiring local Codex config..."
  PAIRPUTER_AWS_REGION="${AWS_REGION}" PAIRPUTER_STACK_NAME="${STACK_NAME}" \
    "${SCRIPT_DIR}/wire-codex.sh" || {
      echo "==> Codex wiring step failed (non-fatal). Wire manually with McpEndpoint + CodexClientId," >&2
      echo "    or re-run: substrate/wire-codex.sh" >&2
    }
fi
