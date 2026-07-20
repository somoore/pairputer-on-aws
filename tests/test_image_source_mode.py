from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class ImageSourceModeTests(unittest.TestCase):
    """Public (day 0, 1-click) vs Private (day 1, BYO) image mode is wired coherently."""

    def setUp(self):
        self.root = read_text("substrate/cloudformation/pairputer.yaml")
        self.agentcore = read_text("substrate/cloudformation/nested/agentcore.yaml")

    def test_image_source_defaults_to_public(self):
        # The 1-click default must be Public (zero inputs).
        self.assertIn("ImageSource:", self.root)
        idx = self.root.index("ImageSource:")
        block = self.root[idx:idx + 400]
        self.assertIn("Default: Public", block)
        self.assertIn("AllowedValues: [Public, Private]", block)

    def test_private_mode_blank_uri_triggers_autocopy(self):
        # Private mode + a blank private URI copies the signed public image into private ECR (per-image).
        self.assertIn("CopyMcpImage:", self.root)
        self.assertIn("CopyRelayImage:", self.root)
        self.assertIn("CopyAnyImage:", self.root)
        # The copier stack is created ONLY when a copy is needed (never in Public mode).
        self.assertIn("ImageCopyStack:", self.root)
        self.assertIn("Condition: CopyAnyImage", self.root)

    def test_effective_uris_are_mode_selected(self):
        # Effective image = public digest | user's private URI | auto-copied private digest.
        self.assertIn("!If [CopyRelayImage, !GetAtt ImageCopyStack.Outputs.RelayPrivateUri, !Ref PrivateRelayContainerUri]", self.root)
        self.assertIn("!If [CopyMcpImage, !GetAtt ImageCopyStack.Outputs.McpPrivateUri, !Ref PrivateMcpContainerUri]", self.root)
        # Both image-source modes use the API-backed runtime provider: the native CFN handler can
        # reject valid AgentCore updates after the runtime has accumulated versions.
        self.assertIn('UseCustomRuntime: "true"', self.root)

    def test_copier_verifies_before_copy_and_is_scoped(self):
        # The copier cosign-verifies the public signed image BEFORE crane-copying it.
        copier = read_text("substrate/cloudformation/nested/image-copy.yaml")
        self.assertIn("cosign verify", copier)
        self.assertIn("verify-attestation --type slsaprovenance", copier)
        self.assertIn("crane copy", copier)
        # Push role scoped to the created repos, not "*".
        self.assertIn("!GetAtt McpPrivateRepo.Arn", copier)
        self.assertNotIn('- Resource: "*"\n', copier.replace("ecr:GetAuthorizationToken", "TOKEN"))

    def test_public_uris_are_public_ecr_only(self):
        # Public-mode defaults + pattern must be public.ecr.aws @sha256 (not private).
        self.assertIn("public.ecr.aws/b6x6x7v3/pairputer-mcp@sha256:", self.root)
        self.assertIn("public.ecr.aws/b6x6x7v3/pairputer-stateful-relay@sha256:", self.root)

    def test_agentcore_has_both_native_and_custom_runtime(self):
        # Native resource gated to Private; custom resource gated to Public.
        self.assertIn("Type: AWS::BedrockAgentCore::Runtime", self.agentcore)
        self.assertIn("Condition: UseNative", self.agentcore)
        self.assertIn("Type: Custom::PairputerAgentCoreRuntime", self.agentcore)
        self.assertIn("Condition: UseCustom", self.agentcore)

    def test_custom_runtime_role_is_least_privilege(self):
        # The custom-runtime Lambda role passes only the controller role, scoped to AgentCore.
        self.assertIn("iam:PassedToService: bedrock-agentcore.amazonaws.com", self.agentcore)
        self.assertIn("bedrock-agentcore:CreateAgentRuntime", self.agentcore)
        # It must NOT grant broad admin.
        self.assertNotIn("bedrock-agentcore:*", self.agentcore)

    def test_outputs_select_runtime_by_mode(self):
        # McpRuntimeId/Arn/Endpoint resolve from whichever runtime exists.
        self.assertIn("!If [UseCustom, !GetAtt McpRuntimeCustom.RuntimeId, !Ref McpRuntime]", self.agentcore)


class BundleReferenceCapsuleTests(unittest.TestCase):
    """The DOOM reference capsule is bundled by default but can be unchecked for a bare substrate."""

    def setUp(self):
        self.root = read_text("substrate/cloudformation/pairputer.yaml")
        self.server = read_text("substrate/mcp-server/server.py")

    def test_bundle_defaults_true(self):
        idx = self.root.index("BundleReferenceCapsule:")
        block = self.root[idx:idx + 400]
        self.assertIn('Default: "true"', block)
        self.assertIn('AllowedValues: ["true", "false"]', block)

    def test_capsule_wiring_is_conditional(self):
        # BundleCapsule gates the build + every capsule value handed to the nested stacks.
        self.assertIn("BundleCapsule: !Equals [!Ref BundleReferenceCapsule", self.root)
        # Empty substrate => empty registry, empty default ARN, placeholder IAM ARN.
        self.assertIn('- "{}"', self.root)                       # CapsuleRegistryJson empty
        self.assertIn("microvm-image:pairputer-no-capsule", self.root)  # IAM placeholder
        # DOOM outputs only exist when bundling.
        self.assertIn("Condition: BundleCapsule", self.root)

    def test_alb_opened_to_cloudfront_vpc_origin_sg(self):
        # CloudFront VPC-origin traffic arrives from the AWS-created "CloudFront-VPCOrigins-Service-SG",
        # NOT the VPC CIDR or edge prefix list. Without an ALB-SG rule referencing it, requests die before
        # the ALB (RequestCount=0). A custom resource wires it after the VPC origin exists.
        relay = read_text("substrate/cloudformation/nested/relay.yaml")
        self.assertIn("Custom::PairputerAlbCloudFrontOriginSg", relay)
        self.assertIn("CloudFront-VPCOrigins-Service-SG", relay)
        self.assertIn("DependsOn: RelayVpcOrigin", relay)  # SG only exists after the origin
        self.assertIn("AuthorizeSecurityGroupIngress", relay)
        # idempotent (tolerates duplicate rule) + best-effort skip if SG absent
        self.assertIn("InvalidPermission.Duplicate", relay)
        # SG mutations scoped to this VPC.
        self.assertIn("ec2:Vpc", relay)

    def test_microvm_reaper_prevents_stuck_teardown(self):
        # A running/suspended MicroVM pins the image and blocks DeleteMicrovmImage. A custom resource must
        # terminate MicroVMs on stack DELETE, BEFORE the image is deleted (DependsOn the image => reverse
        # order on delete). This is the foolproof, tooling-free fix for 1-click users.
        mv = read_text("capsules/nested/capsule-stack.yaml")
        self.assertIn("MicrovmReaper:", mv)
        self.assertIn("Type: Custom::PairputerMicrovmReaper", mv)
        self.assertIn("DependsOn: CapsuleMicrovmImage", mv)  # runs before the image delete
        self.assertIn("lambda:TerminateMicrovm", mv)
        self.assertIn("lambda:ListMicrovms", mv)
        # Delete-only, and must never block teardown (best-effort SUCCESS on error).
        self.assertIn('event["RequestType"] != "Delete"', mv)
        self.assertIn("reaper best-effort", mv)
        # IAM scoped to the image ARN, not "*".
        self.assertIn("microvm-image:${CapsuleImageName}", mv)

    def test_server_tolerates_empty_registry(self):
        # server.py must not require PAIRPUTER_IMAGE_ARN, and must report "no capsules" cleanly.
        self.assertNotIn('os.environ["PAIRPUTER_IMAGE_ARN"]', self.server)
        self.assertIn("no capsules are deployed", self.server)

    def test_network_params_auto_resolve_for_console(self):
        # A console 1-click user never runs deploy.sh, so blank fck-nat AMI + blank VpcCidr must be
        # resolved in-stack, and the param text must say "leave blank" (not "deploy.sh resolves it").
        r = self.root
        # both resolver conditions + custom resources exist
        self.assertIn("ResolveFckNatAmi: !And", r)
        self.assertIn("ResolveVpcCidr: !And", r)
        self.assertIn("Type: Custom::PairputerVpcCidr", r)
        self.assertIn("ec2:DescribeVpcs", r)
        # blank -> resolved value threaded into the network stack
        self.assertIn("!If [ResolveVpcCidr, !GetAtt VpcCidrLookup.Cidr, !Ref VpcCidr]", r)
        self.assertIn("!If [ResolveFckNatAmi, !GetAtt FckNatAmi.ImageId, !Ref FckNatAmiId]", r)
        # honest, non-deploy.sh-implying text
        self.assertNotIn("deploy.sh resolves it automatically", r)
        self.assertIn("LEAVE BLANK", r)

    def test_tool_output_is_clean_not_leaked(self):
        # play/session/lifecycle tools must return a CallToolResult (clean text line for the chat) with the
        # full payload in structuredContent (for the widget) — NOT a bare dict that Codex renders as raw JSON.
        s = self.server
        self.assertIn("from mcp.types import CallToolResult, ImageContent, TextContent", s)
        self.assertIn("def _widget_result(", s)
        self.assertIn("structuredContent=payload", s)
        # the play tools return the wrapped result
        self.assertIn("def play_capsule(ctx: Context, image_id: str = \"\", memory_mib: int = 0) -> CallToolResult:", s)
        # play_capsule launches the VM synchronously (_play) and returns the full session payload —
        # it must NOT depend on the widget's follow-up callTool, which Codex does not reliably deliver.
        self.assertIn("payload = _mark_explicit_open(_play(_caller_identity(ctx), cid))", s)
        self.assertIn("_mark_explicit_open(payload)", s)
        self.assertIn('_widget_result(payload, image_id=cid, state="RUNNING")', s)
        # friendly state mapping (SUSPENDED -> Frozen, etc.)
        self.assertIn('"SUSPENDED": "Frozen"', s)

    def test_read_screen_returns_image_content_block(self):
        # capsule_read_screen must return a proper MCP image content block so clients RENDER the frame —
        # not the bridge's {"format","b64"} dict serialized as base64 text (which no client displays).
        s = self.server
        self.assertIn("def capsule_read_screen(ctx: Context, image_id: str = \"\") -> CallToolResult:", s)
        idx = s.index("def capsule_read_screen(")
        body = s[idx:idx + 900]
        self.assertIn("ImageContent(type=\"image\"", body)
        self.assertIn("mimeType=mime", body)

    def test_registry_supports_capsule_metadata(self):
        # registry accepts both id->"arn" (legacy) and id->{arn,name,description}; server normalizes.
        s = self.server
        self.assertIn("def _normalize_registry(", s)
        self.assertIn('reg[image_id]["arn"]', s)  # arn access through the object form (effective registry)
        self.assertIn("def _capsule_name(", s)
        self.assertIn("def list_capsules(", s)  # friendly picker foundation
        # CFN emits the enriched object form from the selected reference capsule parameters; it must not
        # relabel Agent DOOM (or a future reference cartridge) as the old Hellbox default.
        root = read_text("substrate/cloudformation/pairputer.yaml")
        self.assertIn('"${I}":{"arn":"${A}","name":"${N}","description":"${D}"}', root)
        self.assertIn("I: !Ref ReferenceCapsuleId", root)
        self.assertIn("N: !Ref ReferenceCapsuleName", root)

    def test_tools_are_capsule_agnostic(self):
        # Generic tools exist; DOOM-named tools remain only as deprecated aliases.
        self.assertIn("def play_capsule(", self.server)
        self.assertIn("def capsule_state(", self.server)
        self.assertIn("def _default_image_id(", self.server)
        # No tool hardcodes image_id="doom" as a default anymore (aliases resolve "doom" explicitly).
        self.assertNotIn('image_id: str = "doom"', self.server)
        # play_doom/doom_state kept as back-compat aliases, marked deprecated — but now registered
        # ONLY when PAIRPUTER_DEPRECATED_ALIASES is set (default off), so they don't cost tools/list
        # context every turn. The functions stay defined; only registration is gated.
        self.assertIn("def play_doom(", self.server)
        self.assertIn("Deprecated alias", self.server)
        self.assertIn("_DEPRECATED_ALIASES", self.server)
        self.assertIn("@_deprecated_alias_tool(", self.server)
        # each of the 4 aliases uses the gated decorator, not @mcp.tool
        for alias in ("play_doom", "play_image", "doom_state", "list_images"):
            idx = self.server.index(f"def {alias}(")
            preceding = self.server[max(0, idx - 500):idx]
            self.assertIn("_deprecated_alias_tool", preceding,
                          f"{alias} must use the gated decorator, not @mcp.tool")


if __name__ == "__main__":
    unittest.main()
