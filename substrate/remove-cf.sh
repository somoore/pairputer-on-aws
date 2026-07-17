#!/usr/bin/env bash
# Re-exec under bash if invoked as `sh <script>` (uses bash-only features). Avoids a cryptic syntax error.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
#
# remove-cf.sh — completely delete the pairputer CloudFormation stack.
#
# Deleting the root stack deletes EVERY nested stack automatically (however many there
# are — CloudFormation discovers and tears them down in dependency order; this script
# never hardcodes the nested-stack list). It drives that deletion, waits for it, and
# (optionally) removes the artifact/ECR resources CloudFormation does not own.
#
# Usage:
#   ./remove-cf.sh                 # delete the stack (keeps artifact bucket + ECR repos)
#   ./remove-cf.sh --delete-bucket # also empty + delete the CFN artifact S3 bucket
#   ./remove-cf.sh --delete-ecr    # also force-delete the ECR repos
#   ./remove-cf.sh --all           # stack + artifact bucket + ECR repos
#   ./remove-cf.sh --yes           # skip the confirmation prompt
#
# Credentials/region use the standard AWS chain (see lib/aws-env.sh).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse args first, so --help and validation work without AWS credentials.
DELETE_BUCKET="false"
DELETE_ECR="false"
ASSUME_YES="false"
for arg in "$@"; do
  case "${arg}" in
    --delete-bucket) DELETE_BUCKET="true" ;;
    --delete-ecr)    DELETE_ECR="true" ;;
    --all)           DELETE_BUCKET="true"; DELETE_ECR="true" ;;
    --yes|-y)        ASSUME_YES="true" ;;
    -h|--help)
      sed -n '4,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

# shellcheck source=lib/aws-env.sh
source "${SCRIPT_DIR}/lib/aws-env.sh"
hb_require_aws || exit 1

STACK_NAME="${PAIRPUTER_STACK_NAME:-pairputer}"
ECR_REPOS=("pairputer-mcp" "pairputer-stateful-relay")
ARTIFACT_BUCKET="${PAIRPUTER_CFN_BUCKET:-pairputer-cfn-artifacts-${AWS_ACCOUNT_ID}-${AWS_REGION}}"
DOOM_IMAGE_NAME="${PAIRPUTER_DOOM_IMAGE_NAME:-${STACK_NAME}-doom}"

# delete_orphan_microvm_image: best-effort delete of an AWS::Lambda::MicrovmImage that CloudFormation
# retained (the flaky resource sometimes refuses to delete, so force_delete_stack skips it). There is
# no `aws lambda-microvms` CLI service, but the JS SDK (@aws-sdk/client-lambda-microvms, present after
# a relay build) has DeleteMicrovmImage. Try that via node; if node or the SDK is absent, warn but do
# NOT fail — deploy.sh now ADOPTS an existing same-name image, so a leftover orphan is not fatal.
delete_orphan_microvm_image() {
  # $1 = image NAME (defaults to the DOOM image). Capsule cartridges each own a distinct image
  # (microvm-image:<capsule-id>), so teardown must be able to target ANY of them, not just DOOM —
  # a hardcoded DOOM name silently no-op'd on a stuck workbench image (2026-07-16 teardown).
  local image_name="${1:-${DOOM_IMAGE_NAME}}"
  local arn
  arn="$(aws resourcegroupstaggingapi get-resources --region "${AWS_REGION}" \
    --resource-type-filters lambda \
    --query "ResourceTagMappingList[?ends_with(ResourceARN, ':microvm-image:${image_name}')].ResourceARN | [0]" \
    --output text 2>/dev/null || true)"
  if [[ -z "${arn}" || "${arn}" == "None" ]]; then
    return 0  # nothing orphaned
  fi
  echo ""
  echo "==> Orphaned MicroVM image still exists: ${arn}"
  local sdk="${SCRIPT_DIR}/stateful-relay/node_modules/@aws-sdk/client-lambda-microvms"
  if command -v node >/dev/null 2>&1 && [[ -d "${sdk}" ]]; then
    echo "    Terminating any MicroVMs on it, then deleting the image via the JS SDK..."
    # An image can't be deleted while any MicroVM (incl. a leftover SUSPENDED one) references it,
    # so terminate them first and wait for them to reach TERMINATED before deleting.
    if AWS_REGION="${AWS_REGION}" HB_IMG_ARN="${arn}" HB_SDK="${sdk}" node -e '
        const {LambdaMicrovmsClient,ListMicrovmsCommand,TerminateMicrovmCommand,DeleteMicrovmImageCommand,GetMicrovmImageCommand}=require(process.env.HB_SDK);
        const c=new LambdaMicrovmsClient({region:process.env.AWS_REGION});
        const arn=process.env.HB_IMG_ARN, sleep=ms=>new Promise(r=>setTimeout(r,ms));
        const gone=e=>e.name==="ResourceNotFoundException"||e.name==="NotFoundException";
        const imageState=async()=>{ try{ return (await c.send(new GetMicrovmImageCommand({imageIdentifier:arn}))).state; }catch(e){ return gone(e)?"GONE":"ERR"; } };
        (async()=>{
          try{
            const l=await c.send(new ListMicrovmsCommand({imageIdentifier:arn}));
            for(const vm of (l.items||[])){
              if(vm.state!=="TERMINATED"){
                try{ await c.send(new TerminateMicrovmCommand({microvmIdentifier:vm.microvmId})); }catch(_){}
              }
            }
            for(let i=0;i<18;i++){
              const s=await c.send(new ListMicrovmsCommand({imageIdentifier:arn}));
              if((s.items||[]).every(v=>v.state==="TERMINATED")) break;
              await sleep(5000);
            }
            // A wedged image is NOT cleared by "delete accepted": DeleteMicrovmImage returns 200, the
            // image sits in DELETING ~90s, then flips back to DELETE_FAILED on its own (AWS-side "did
            // not stabilize"). The only lever that eventually clears it is RE-ISSUING the whole-image
            // delete each time it lands back in DELETE_FAILED — individual version deletes are refused
            // while it is DELETION_FAILED. So: issue, then POLL to GONE; on a flip back to
            // DELETE_FAILED, re-issue. Up to ~10 min. (2026-07-16: the workbench image needed this.)
            const deadline=Date.now()+10*60*1000;
            let issued=false;
            while(Date.now()<deadline){
              const st=await imageState();
              if(st==="GONE"){ console.error("    deleted."); process.exit(0); }
              if(st==="DELETE_FAILED" || st==="DELETION_FAILED" || !issued){
                try{ await c.send(new DeleteMicrovmImageCommand({imageIdentifier:arn})); issued=true; console.error("    (re)issued DeleteMicrovmImage; state was "+st); }
                catch(e){ if(gone(e)){ console.error("    already gone."); process.exit(0); } console.error("    delete call failed ("+(e.name||e.message)+"); will retry"); }
              }
              await sleep(15000);
            }
            throw new Error("image did not clear within 10 min (still wedged AWS-side)");
          }catch(e){ console.error("    delete failed: "+(e.name||e.message)); process.exit(1); }
        })();
      ' 2>&1; then
      echo "    Orphaned image deleted."
    else
      echo "    Could not delete it automatically; it is harmless (deploy.sh reuses it)." >&2
    fi
  else
    echo "    node or @aws-sdk/client-lambda-microvms not available to delete it." >&2
    echo "    It is harmless: a redeploy ADOPTS this same-name image instead of failing." >&2
    echo "    To force a rebuild later, delete it, or set PAIRPUTER_FORCE_REBUILD_DOOM_IMAGE=true." >&2
  fi
}

# sweep_microvms_on_image: terminate every non-TERMINATED MicroVM on the DOOM image and wait for
# TERMINATED — but do NOT delete the image (CloudFormation still owns that). This is the safety net for
# the reaper's blind spot: the in-stack MicrovmReaper runs ONCE at the start of a delete, so a VM that is
# frozen AFTER that sweep (or a delete that is retried/retained around the reaper) leaves a SUSPENDED VM
# pinning the image → DeleteMicrovmImage fails with GeneralServiceException. Calling this before each
# stack-delete retry and before any --retain of the image closes that hole at the layer that retries.
# Best-effort: never fails the caller (returns 0) so it can't itself block teardown. Needs the JS SDK.
sweep_microvms_on_image() {
  # $1 = image NAME (defaults to the DOOM image); capsule images pass their own id. See the
  # delete_orphan_microvm_image note above — a hardcoded DOOM name can't clear a capsule's VMs.
  local image_name="${1:-${DOOM_IMAGE_NAME}}"
  local arn
  arn="$(aws resourcegroupstaggingapi get-resources --region "${AWS_REGION}" \
    --resource-type-filters lambda \
    --query "ResourceTagMappingList[?ends_with(ResourceARN, ':microvm-image:${image_name}')].ResourceARN | [0]" \
    --output text 2>/dev/null || true)"
  [[ -z "${arn}" || "${arn}" == "None" ]] && return 0   # image already gone
  local sdk="${SCRIPT_DIR}/stateful-relay/node_modules/@aws-sdk/client-lambda-microvms"
  command -v node >/dev/null 2>&1 && [[ -d "${sdk}" ]] || return 0   # can't sweep without the SDK
  AWS_REGION="${AWS_REGION}" HB_IMG_ARN="${arn}" HB_SDK="${sdk}" node -e '
      const {LambdaMicrovmsClient,ListMicrovmsCommand,TerminateMicrovmCommand}=require(process.env.HB_SDK);
      const c=new LambdaMicrovmsClient({region:process.env.AWS_REGION});
      const arn=process.env.HB_IMG_ARN, sleep=ms=>new Promise(r=>setTimeout(r,ms));
      (async()=>{
        try{
          const l=await c.send(new ListMicrovmsCommand({imageIdentifier:arn}));
          const live=(l.items||[]).filter(v=>v.state!=="TERMINATED");
          if(!live.length) return process.exit(0);
          console.error("    Sweeping "+live.length+" MicroVM(s) still on the image ("+live.map(v=>v.state).join(",")+")...");
          for(const vm of live){ try{ await c.send(new TerminateMicrovmCommand({microvmIdentifier:vm.microvmId})); }catch(_){} }
          for(let i=0;i<24;i++){
            const s=await c.send(new ListMicrovmsCommand({imageIdentifier:arn}));
            if((s.items||[]).every(v=>v.state==="TERMINATED")){ console.error("    All MicroVMs TERMINATED."); break; }
            await sleep(5000);
          }
          process.exit(0);
        }catch(e){ console.error("    sweep note: "+(e.name||e.message)); process.exit(0); }
      })();
    ' 2>&1 || true
  return 0
}

echo "==> Region:         ${AWS_REGION}"
echo "==> Stack:          ${STACK_NAME} (root + all nested stacks)"
echo "==> Delete bucket:  ${DELETE_BUCKET} (${ARTIFACT_BUCKET})"
echo "==> Delete ECR:     ${DELETE_ECR}"

if [[ "${ASSUME_YES}" != "true" ]]; then
  echo ""
  read -r -p "DELETE stack '${STACK_NAME}' and all nested stacks in '${AWS_REGION}'? [y/N] " reply
  if [[ ! "${reply}" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
  fi
fi

echo ""
echo "==> NOTE: terminate active DOOM MicroVMs first if you have live sessions"
echo "    (widget Trash button or the trash_microvm MCP tool). AgentCore idle policy"
echo "    reaps idle ones, but an in-flight MicroVM can slow a clean delete."

# Capture the ACTUAL MicroVM image ARN from the stack's DoomImageArn output BEFORE we delete anything.
# The name is a stack parameter (default hellbox-doom, but a rebuild may rename it, e.g. hellbox-doom-r2),
# so guessing "${STACK_NAME}-doom" misses it and orphan cleanup silently no-ops (bit us in wall #15/17).
# Fall back to the guessed name only if the output is unavailable.
DOOM_IMAGE_ARN="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='DoomImageArn'].OutputValue | [0]" --output text 2>/dev/null || true)"
if [[ -n "${DOOM_IMAGE_ARN}" && "${DOOM_IMAGE_ARN}" != "None" ]]; then
  DOOM_IMAGE_NAME="${DOOM_IMAGE_ARN##*:}"   # ARN tail = the real image name
fi

# delete_and_wait: delete a (nested, root, OR capsule) stack and wait. Returns 0 on gone,
# 1 on DELETE_FAILED. "gone" includes a describe that errors with "does not exist".
delete_and_wait() {
  local name="$1"; shift  # any extra args (e.g. --retain-resources ...) pass through
  aws cloudformation delete-stack --stack-name "${name}" --region "${AWS_REGION}" "$@" >/dev/null 2>&1 || true
  if aws cloudformation wait stack-delete-complete --stack-name "${name}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    return 0
  fi
  # wait fails both when the stack DELETE_FAILED and when it's already gone; treat gone as success.
  if ! aws cloudformation describe-stacks --stack-name "${name}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# stuck_leaf_resources: logical ids of DELETE_FAILED resources in a stack that are NOT
# themselves nested stacks (i.e. real leaf resources we can retain).
stuck_leaf_resources() {
  aws cloudformation describe-stack-resources --stack-name "$1" --region "${AWS_REGION}" \
    --query "StackResources[?ResourceStatus=='DELETE_FAILED' && ResourceType!='AWS::CloudFormation::Stack'].LogicalResourceId" \
    --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$' || true
}

# stuck_nested_stacks: PhysicalResourceId (real stack name) of DELETE_FAILED nested stacks.
stuck_nested_stacks() {
  aws cloudformation describe-stack-resources --stack-name "$1" --region "${AWS_REGION}" \
    --query "StackResources[?ResourceStatus=='DELETE_FAILED' && ResourceType=='AWS::CloudFormation::Stack'].PhysicalResourceId" \
    --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$' || true
}

# force_delete_stack: delete a stack, tolerating the flaky AWS::Lambda::MicrovmImage. Try once, retry
# on DELETE_FAILED (many failures are transient); recurse into a stuck NESTED stack; then fall back to
# --retain-resources on any stuck LEAF. Now used for the ROOT *and* each CAPSULE cartridge stack —
# previously capsule stacks got a naive delete+warn and left a wedged image + orphan VM for the human
# to hand-clean (2026-07-16 teardown). $2 = the image NAME this stack owns (so the VM sweep + orphan
# delete target the RIGHT image — capsules own microvm-image:<capsule-id>, not the DOOM image).
force_delete_stack() {
  local name="$1" image_name="${2:-${DOOM_IMAGE_NAME}}"
  echo "    Deleting ${name} (can take minutes; CloudFront/WAF are slow)..."
  delete_and_wait "${name}" && { echo "    Deleted: ${name}"; return 0; }

  # Retry a few times with a wait between attempts. The AgentCore runtime can sit in DELETING for
  # minutes (a 409 "currently being modified / Current status: DELETING" comes back if you delete
  # again too fast), and AWS::Lambda::MicrovmImage flakes with a transient GeneralServiceException —
  # both usually clear if you just wait and retry rather than immediately retaining. wall #15/17.
  local attempt
  for attempt in 1 2 3; do
    echo "    ${name} did not delete cleanly; waiting for in-flight deletes to settle, retry ${attempt}/3..." >&2
    sleep 30
    # Safety net for the run-once reaper: a VM frozen AFTER the reaper's single sweep (or a retry that
    # runs past it) leaves a SUSPENDED VM pinning the image, so DeleteMicrovmImage keeps failing. Sweep
    # every VM off the image before each retry so CFN's own image delete can then succeed. wall #15.
    sweep_microvms_on_image "${image_name}"
    delete_and_wait "${name}" && { echo "    Deleted on retry ${attempt}: ${name}"; return 0; }
  done

  # If the blocker is a nested stack, recurse into it (retain happens at the leaf level there),
  # then retry the parent.
  local child
  for child in $(stuck_nested_stacks "${name}"); do
    echo "    Blocked by nested stack ${child}; forcing it down first..." >&2
    force_delete_stack "${child}" || true
  done
  delete_and_wait "${name}" && { echo "    Deleted after clearing nested stacks: ${name}"; return 0; }

  # Otherwise retain stuck leaf resources so the rest of the stack tears down.
  local stuck
  stuck="$(stuck_leaf_resources "${name}")"
  if [[ -n "${stuck}" ]]; then
    # If the MicroVM image is what's stuck, it's almost always a VM still pinning it. Sweep the VMs and
    # try one more clean delete FIRST — deleting the image is far better than retaining it (retain leaves
    # both the image AND its pinning VM orphaned, which is exactly what happened in wall #15/17).
    if echo "${stuck}" | grep -qiE 'Microvm|DoomImage'; then
      echo "    Stuck resource looks like the MicroVM image; sweeping VMs and retrying a clean delete..." >&2
      sweep_microvms_on_image "${image_name}"
      delete_and_wait "${name}" && { echo "    Deleted after VM sweep: ${name}"; return 0; }
      # Still stuck with no VM pinning it => the image is genuinely WEDGED: it flips DELETING ->
      # DELETE_FAILED on its own (AWS-side "did not stabilize"), and individual version deletes are
      # then REJECTED ("DELETION_FAILED -> UPDATING"). Re-issuing the whole-image DeleteMicrovmImage a
      # few times is the only lever that eventually clears it. 2026-07-16: the workbench image needed
      # exactly this before --retain-resources could even land.
      echo "    Image still stuck with no VM pinning it; re-issuing DeleteMicrovmImage to unwedge it..." >&2
      delete_orphan_microvm_image "${image_name}"
      delete_and_wait "${name}" && { echo "    Deleted after unwedging the image: ${name}"; return 0; }
      stuck="$(stuck_leaf_resources "${name}")"   # re-check; the image may now delete cleanly
    fi
  fi
  if [[ -n "${stuck}" ]]; then
    echo "    Still stuck after retry: $(echo "${stuck}" | tr '\n' ' ')" >&2
    echo "    Retaining stuck resource(s) so the rest of the stack can delete..." >&2
    # shellcheck disable=SC2086  # word-splitting is intended: one --retain-resources list
    if delete_and_wait "${name}" --retain-resources ${stuck}; then
      echo "    Deleted (retained: $(echo "${stuck}" | tr '\n' ' '))" >&2
      RETAINED_RESOURCES="${RETAINED_RESOURCES}${RETAINED_RESOURCES:+ }${stuck//$'\n'/ }"
      # Keep hammering the retained image out-of-band so it doesn't linger as an orphan (harmless
      # cost-wise, but it fills the ~50-version quota and a redeploy would adopt a stale image).
      if echo "${stuck}" | grep -qiE 'Microvm|DoomImage'; then
        delete_orphan_microvm_image "${image_name}"
      fi
      return 0
    fi
  fi
  return 1
}

# Cartridge capsules are their OWN stacks (pairputer-capsule-<id>), not nested under the
# root — a root delete leaves them (and their billable MicroVM image, possibly with a
# frozen VM pinning it) silently alive. Delete them FIRST, with the SAME force_delete_stack
# recovery the root gets (retry + VM sweep + unwedge + retain), targeting each capsule's OWN image.
CAPSULE_STACKS="$(aws cloudformation list-stacks --region "${AWS_REGION}" \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE DELETE_FAILED \
  --query "StackSummaries[?starts_with(StackName, '${STACK_NAME}-capsule-')].StackName" \
  --output text 2>/dev/null | tr '\t' '\n' | sort -u || true)"
RETAINED_RESOURCES=""
if [[ -n "${CAPSULE_STACKS}" ]]; then
  echo ""
  echo "==> Capsule cartridge stacks (deleted before the substrate):"
  echo "${CAPSULE_STACKS}" | sed 's/^/      - /'
  while IFS= read -r cs; do
    [[ -n "${cs}" ]] || continue
    # pairputer-capsule-<id> -> the capsule owns microvm-image:<id>. Strip the "<stack>-capsule-"
    # prefix to recover the id so the VM sweep + image delete target the RIGHT image.
    capsule_image="${cs#"${STACK_NAME}-capsule-"}"
    if ! force_delete_stack "${cs}" "${capsule_image}"; then
      echo "    ERROR: capsule stack ${cs} could not be deleted even after retry + retain." >&2
      echo "           Its MicroVM image (microvm-image:${capsule_image}) may be wedged AWS-side;" >&2
      echo "           re-run remove-cf.sh in a few minutes to let it settle." >&2
      CAPSULE_DELETE_FAILED=1
    fi
  done <<< "${CAPSULE_STACKS}"
fi

# List the nested stacks up front so the user sees exactly what is being removed.
if aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo ""
  echo "==> Nested stacks that will be deleted with the root:"
  aws cloudformation describe-stack-resources \
    --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
    --query "StackResources[?ResourceType=='AWS::CloudFormation::Stack'].LogicalResourceId" \
    --output text 2>/dev/null | tr '\t' '\n' | sed 's/^/      - /' || true

  echo ""
  echo "==> Deleting stack..."
  # RETAINED_RESOURCES was initialized before the capsule loop; do NOT reset it here or a capsule's
  # retained image would drop out of the final summary.
  if force_delete_stack "${STACK_NAME}" "${DOOM_IMAGE_NAME}"; then
    echo "    Stack and all nested stacks deleted."
    if [[ -n "${RETAINED_RESOURCES}" ]]; then
      echo ""
      echo "==> NOTE: these resources were RETAINED (they refused to delete and were skipped):"
      echo "      ${RETAINED_RESOURCES}"
      echo "    The stacks are gone. A retained AWS::Lambda::MicrovmImage is harmless (no ongoing"
      echo "    cost/compute); deploy.sh reuses a same-name image instead of failing on redeploy."
      # Best-effort: actually delete the orphaned image so a fresh build is possible next time.
      if [[ "${RETAINED_RESOURCES}" == *Microvm* || "${RETAINED_RESOURCES}" == *Doom* ]]; then
        delete_orphan_microvm_image
      fi
    fi
  else
    echo "ERROR: stack deletion did not complete cleanly even after retry + retain. Status:" >&2
    aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
      --query 'Stacks[0].StackStatus' --output text >&2 2>/dev/null || true
    echo "" >&2
    echo "    Another resource may be blocking deletion (a non-empty S3 bucket, or an in-use" >&2
    echo "    network resource). Inspect events, resolve it, and re-run:" >&2
    echo "      aws cloudformation describe-stack-events --stack-name ${STACK_NAME} --region ${AWS_REGION}" >&2
    exit 1
  fi
else
  echo "==> Stack '${STACK_NAME}' not found; already deleted."
fi

if [[ "${DELETE_BUCKET}" == "true" ]]; then
  echo ""
  echo "==> Emptying and deleting artifact bucket '${ARTIFACT_BUCKET}'..."
  if aws s3api head-bucket --bucket "${ARTIFACT_BUCKET}" >/dev/null 2>&1; then
    # Remove all objects (and versions, if the bucket happens to be versioned).
    aws s3 rm "s3://${ARTIFACT_BUCKET}" --recursive >/dev/null 2>&1 || true
    aws s3api delete-bucket --bucket "${ARTIFACT_BUCKET}" --region "${AWS_REGION}" >/dev/null 2>&1 \
      && echo "    Bucket deleted." \
      || echo "    Could not delete bucket (it may be versioned or non-empty); remove it manually."
  else
    echo "    Bucket not found; skipping."
  fi
else
  echo ""
  echo "==> Leaving artifact bucket in place (pass --delete-bucket to remove it)."
fi

if [[ "${DELETE_ECR}" == "true" ]]; then
  for repo in "${ECR_REPOS[@]}"; do
    echo "==> Force-deleting ECR repository '${repo}'..."
    if aws ecr describe-repositories --repository-names "${repo}" --region "${AWS_REGION}" >/dev/null 2>&1; then
      aws ecr delete-repository --repository-name "${repo}" --region "${AWS_REGION}" --force >/dev/null
      echo "    Deleted."
    else
      echo "    Not found; skipping."
    fi
  done
else
  echo "==> Leaving ECR repositories in place (pass --delete-ecr to remove them)."
fi

# CloudWatch log groups survive every stack delete (AgentCore runtimes, CodeBuild image
# builds, custom-resource Lambdas) and accumulate across deploy/teardown cycles — 60+
# observed after a week of iteration. Sweep anything carrying the stack name under the
# same flag as the other non-CFN leftovers.
if [[ "${DELETE_BUCKET}" == "true" ]]; then
  echo "==> Deleting CloudWatch log groups containing '${STACK_NAME}'..."
  LOG_GROUPS="$(aws logs describe-log-groups --region "${AWS_REGION}" \
    --query "logGroups[?contains(logGroupName, '${STACK_NAME}')].logGroupName" \
    --output text 2>/dev/null | tr '\t' '\n' || true)"
  LG_COUNT=0
  while IFS= read -r lg; do
    [[ -n "${lg}" ]] || continue
    aws logs delete-log-group --log-group-name "${lg}" --region "${AWS_REGION}" >/dev/null 2>&1 && LG_COUNT=$((LG_COUNT+1)) || true
  done <<< "${LOG_GROUPS}"
  echo "    Deleted ${LG_COUNT} log group(s)."
fi

echo ""
if [[ "${CAPSULE_DELETE_FAILED:-0}" == "1" ]]; then
  echo "==> Removal complete EXCEPT a capsule stack whose MicroVM image is wedged AWS-side." >&2
  echo "    The substrate is gone; re-run remove-cf.sh in a few minutes to clear the image." >&2
  exit 1
fi
echo "==> Removal complete."
