#!/usr/bin/env bash
# Re-exec under bash if invoked as `sh <script>` (uses bash-only features). Avoids a cryptic syntax error.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
#
# publish-launch.sh — publish the pairputer CloudFormation templates to a public
# launch bucket and print the 1-click "Launch Stack" console URL.
#
# What it does:
#   1. Ensures a launch bucket exists with a bucket policy that allows s3:GetObject
#      ONLY when the request is made via CloudFormation (aws:CalledVia =
#      cloudformation.amazonaws.com). Direct/anonymous browser or curl GETs are
#      denied; any account's CloudFormation can still read the templates to launch.
#   2. Runs `aws cloudformation package` so the root template's nested TemplateURLs
#      are rewritten to absolute URLs in THIS bucket, then uploads the packaged root
#      + nested templates.
#   3. Prints the console create/review URL with templateURL= pointing at the root.
#
# NOTE: the console "Review" page fetches the template in your browser to render the
# parameter form; because the policy blocks non-CloudFormation reads, that preview
# may not display. The launch itself works (CloudFormation reads the templates). Use
# --public-read if you also want the browser preview / plain public visibility.
#
# Usage:
#   ./publish-launch.sh                      # bucket: pairputer-launch-<account>, region from chain
#   PAIRPUTER_LAUNCH_BUCKET=my-bucket ./publish-launch.sh
#   ./publish-launch.sh --public-read        # world-readable templates instead of CalledVia-only
#
# The templates contain no secrets — every secret is generated inside the deploying
# account at stack-create time.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PUBLIC_READ="false"
for arg in "$@"; do
  case "${arg}" in
    --public-read) PUBLIC_READ="true" ;;
    -h|--help) sed -n '4,35p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

# shellcheck source=lib/aws-env.sh
source "${SCRIPT_DIR}/lib/aws-env.sh"
hb_require_aws || exit 1

TEMPLATE_FILE="${SCRIPT_DIR}/cloudformation/pairputer.yaml"
# Default to the CANONICAL public bucket the README "Launch Stack" button + pairputer.yaml artifact
# defaults point at, so a normal publish keeps that button current. Includes the account number because
# S3 bucket names are globally unique (a bare "pairputer-launch" can't be re-owned by forkers). Forkers
# override with PAIRPUTER_LAUNCH_BUCKET=<their-bucket> (and update their own README URL + template defaults).
LAUNCH_BUCKET="${PAIRPUTER_LAUNCH_BUCKET:-pairputer-launch-932930471665}"
STACK_NAME_DEFAULT="${PAIRPUTER_STACK_NAME:-pairputer}"
PACKAGED="/tmp/${LAUNCH_BUCKET}-launch-packaged.yaml"
ROOT_KEY="templates/pairputer.yaml"

if [[ ! -f "${TEMPLATE_FILE}" ]]; then
  echo "ERROR: template not found at ${TEMPLATE_FILE}" >&2
  exit 1
fi
if [[ "${AWS_REGION}" != "us-east-1" ]]; then
  echo "WARNING: launching outside us-east-1 disables the nested CloudFront WAF" >&2
  echo "         (CLOUDFRONT-scope WAFv2 is us-east-1 only). us-east-1 recommended." >&2
fi

echo "==> Launch bucket:  ${LAUNCH_BUCKET} (${AWS_REGION})"
echo "==> Access policy:  $( [[ "${PUBLIC_READ}" == "true" ]] && echo "public-read" || echo "CloudFormation-only (aws:CalledVia)" )"

# 1. Create bucket if missing. We DO attach a bucket policy, so BlockPublicPolicy /
#    RestrictPublicBuckets must be off; ACLs stay blocked (policy-based access only).
if ! aws s3api head-bucket --bucket "${LAUNCH_BUCKET}" >/dev/null 2>&1; then
  echo "==> Creating bucket..."
  if [[ "${AWS_REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${LAUNCH_BUCKET}" --region "${AWS_REGION}" >/dev/null
  else
    aws s3api create-bucket --bucket "${LAUNCH_BUCKET}" --region "${AWS_REGION}" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
  fi
fi
aws s3api put-public-access-block --bucket "${LAUNCH_BUCKET}" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false" >/dev/null

# 2. Attach the access policy.
if [[ "${PUBLIC_READ}" == "true" ]]; then
  POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadTemplates",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": [
        "arn:aws:s3:::${LAUNCH_BUCKET}/templates/*",
        "arn:aws:s3:::${LAUNCH_BUCKET}/pairputer/microvm-image/*"
      ]
    }
  ]
}
EOF
)
else
  # Allow GetObject only when the call flows through CloudFormation (any account).
  # Verified: blocks anonymous/browser GET (403); permits CloudFormation to read the
  # root AND nested templateURLs AND the DOOM build-context zip during create-stack.
  POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowGetOnlyViaCloudFormation",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": [
        "arn:aws:s3:::${LAUNCH_BUCKET}/templates/*",
        "arn:aws:s3:::${LAUNCH_BUCKET}/pairputer/microvm-image/*"
      ],
      "Condition": {
        "ForAnyValue:StringEquals": { "aws:CalledVia": "cloudformation.amazonaws.com" }
      }
    }
  ]
}
EOF
)
fi
echo "==> Attaching bucket policy..."
aws s3api put-bucket-policy --bucket "${LAUNCH_BUCKET}" --policy "${POLICY}" >/dev/null

# 3. Package: rewrite nested TemplateURLs to absolute URLs in THIS bucket, then upload.
echo "==> Packaging templates (nested TemplateURLs -> absolute S3 URLs in the launch bucket)..."
aws cloudformation package \
  --template-file "${TEMPLATE_FILE}" \
  --s3-bucket "${LAUNCH_BUCKET}" \
  --s3-prefix "templates" \
  --region "${AWS_REGION}" \
  --output-template-file "${PACKAGED}" >/dev/null

echo "==> Uploading packaged root template to s3://${LAUNCH_BUCKET}/${ROOT_KEY}..."
aws s3 cp "${PACKAGED}" "s3://${LAUNCH_BUCKET}/${ROOT_KEY}" --region "${AWS_REGION}" >/dev/null

# 4. Publish the WAD-free DOOM build context zip under pairputer/microvm-image/ so the public 1-click
# bundled-image build (BundleReferenceCapsule=true + ImageSource=Public) can fetch it. package-doom-image.sh
# uploads it to <bucket>/<prefix>/pairputer-doom-context-<treehash>.zip and prints that s3:// URI. The
# reference capsule defaults to agent-doom (PAIRPUTER_REFERENCE_CAPSULE). Skip with PAIRPUTER_SKIP_CONTEXT=1.
if [[ "${PAIRPUTER_SKIP_CONTEXT:-0}" != "1" ]]; then
  echo "==> Publishing DOOM build context to s3://${LAUNCH_BUCKET}/pairputer/microvm-image/..."
  CONTEXT_URI="$("${SCRIPT_DIR}/package-doom-image.sh" "${LAUNCH_BUCKET}" "pairputer/microvm-image" | tail -n1)"
  if [[ "${CONTEXT_URI}" != s3://* ]]; then
    echo "ERROR: package-doom-image.sh did not return an s3:// context URI (got: ${CONTEXT_URI})." >&2
    exit 1
  fi
  echo "    context: ${CONTEXT_URI}"
  echo "    (set DoomCodeArtifactUri=${CONTEXT_URI} + DoomCodeArtifactBucket=${LAUNCH_BUCKET} in pairputer.yaml defaults if this is a new hash)"
fi

# Virtual-hosted-style URL (works for path-style regions too).
if [[ "${AWS_REGION}" == "us-east-1" ]]; then
  TEMPLATE_URL="https://${LAUNCH_BUCKET}.s3.amazonaws.com/${ROOT_KEY}"
else
  TEMPLATE_URL="https://${LAUNCH_BUCKET}.s3.${AWS_REGION}.amazonaws.com/${ROOT_KEY}"
fi
LAUNCH_URL="https://console.aws.amazon.com/cloudformation/home?region=${AWS_REGION}#/stacks/create/review?templateURL=${TEMPLATE_URL}&stackName=${STACK_NAME_DEFAULT}"

echo ""
echo "==> Published."
echo "    Template URL: ${TEMPLATE_URL}"
echo ""
echo "    1-click launch URL (put this behind the README button):"
echo "    ${LAUNCH_URL}"
echo ""
echo "    README markdown:"
echo "    [![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](${LAUNCH_URL})"
