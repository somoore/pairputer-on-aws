"""Security invariants for the hosted Pairputer Workbench topology.

These are deliberately cross-file contract tests.  A change to one nested stack or
runtime must not silently turn the authenticated Agent-Doom-style topology into a
public desktop, media, input, or control plane.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def section(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


class WorkbenchIdentityBoundaryTests(unittest.TestCase):
    def test_interactive_clients_are_admin_only_public_code_clients(self):
        identity = read("substrate/cloudformation/nested/identity.yaml")
        pool = section(identity, "  UserPool:", "  ResourceServer:")
        self.assertIn("AllowAdminCreateUserOnly: true", pool)

        for name, next_marker in (
            ("CodexClient", "  ChatGPTClient:"),
            ("ChatGPTClient", "  ClaudeClient:"),
            ("ClaudeClient", "Outputs:"),
        ):
            client = section(identity, f"  {name}:", next_marker)
            self.assertIn("GenerateSecret: false", client)
            self.assertIn("AllowedOAuthFlows: [code]", client)
            self.assertIn('"pairputer-mcp/invoke"', client)
            self.assertIn("SupportedIdentityProviders: [COGNITO]", client)

    def test_both_agentcore_runtime_paths_require_cognito_jwt(self):
        agentcore = read("substrate/cloudformation/nested/agentcore.yaml")
        native = section(agentcore, "  McpRuntime:", "  CustomRuntimeRole:")
        custom = section(agentcore, "          function spec(p)", "          exports.handler")

        self.assertIn("CustomJWTAuthorizer:", native)
        self.assertIn("DiscoveryUrl: !Ref DiscoveryUrl", native)
        self.assertIn("RequestHeaderAllowlist:", native)
        self.assertIn("- Authorization", native)
        self.assertIn("AllowedScopes:", native)
        self.assertIn("- !Ref RequiredScope", native)
        for client in ("M2MClientId", "CodexClientId", "ChatGPTClientId", "ClaudeClientId"):
            self.assertIn(f"!Ref {client}", native)

        self.assertIn('requestHeaderAllowlist:["Authorization"]', custom)
        self.assertIn("customJWTAuthorizer", custom)
        self.assertIn("discoveryUrl:p.DiscoveryUrl", custom)
        self.assertIn("allowedClients:p.AllowedClients", custom)
        self.assertIn("allowedScopes:p.AllowedScopes", custom)

        custom_resource = section(agentcore, "  McpRuntimeCustom:", "  CallbackRegistrarRole:")
        self.assertIn("AllowedScopes:", custom_resource)
        self.assertIn("- !Ref RequiredScope", custom_resource)
        self.assertEqual(agentcore.count('${M2MClientId}":"m2m"'), 2)
        self.assertNotIn('${M2MClientId}":"codex"', agentcore)

    def test_root_stack_cannot_omit_identity_session_or_signing_stacks(self):
        root = read("substrate/cloudformation/pairputer.yaml")
        resources = section(root, "Resources:", "Outputs:")
        for logical_id in ("SecurityStack", "IdentityStack", "SessionsStack", "RelayStack", "AgentCoreStack"):
            resource = section(resources, f"  {logical_id}:", "\n\n")
            self.assertNotIn("Condition:", resource)

        relay = section(resources, "  RelayStack:", "  AgentCoreStack:")
        self.assertIn("CloudFrontKeyGroupId: !GetAtt SecurityStack.Outputs.CloudFrontKeyGroupId", relay)
        self.assertIn("RelaySessionSecretArn: !GetAtt SecurityStack.Outputs.RelaySessionSecretArn", relay)
        self.assertIn("SessionTableName: !GetAtt SessionsStack.Outputs.SessionTableName", relay)
        agentcore = resources[resources.index("  AgentCoreStack:"):]
        self.assertIn("RequiredScope: !GetAtt IdentityStack.Outputs.M2MScope", agentcore)


class WorkbenchRelayBoundaryTests(unittest.TestCase):
    def test_cloudfront_auth_and_waf_are_enabled_in_the_production_shape(self):
        root = read("substrate/cloudformation/pairputer.yaml")
        relay = read("substrate/cloudformation/nested/relay.yaml")
        waf = read("substrate/cloudformation/nested/cloudfront-waf.yaml")
        distribution = section(relay, "  VideoRelayDistribution:", "Outputs:")

        waf_parameter = section(root, "  EnableCloudFrontWaf:", "  CloudFrontWafRateLimitPerFiveMinutes:")
        self.assertIn('Default: "true"', waf_parameter)
        self.assertIn("WebACLId: !If [HasWebAcl", distribution)
        self.assertIn("TrustedKeyGroups:", distribution)
        for rule in (
            "AWSManagedRulesAmazonIpReputationList",
            "AWSManagedRulesKnownBadInputsRuleSet",
            "AWSManagedRulesAnonymousIpList",
            "AWSManagedRulesCommonRuleSet",
            "BlockMissingRelayAuthParams",
            "PerIpRateLimit",
        ):
            self.assertIn(rule, waf)
        for auth_param in ('SearchString: "t="', 'SearchString: "Policy="',
                           'SearchString: "Signature="', 'SearchString: "Key-Pair-Id="'):
            self.assertIn(auth_param, waf)

    def test_only_cloudfront_vpc_origin_can_reach_private_relay(self):
        relay = read("substrate/cloudformation/nested/relay.yaml")
        alb = section(relay, "  RelayLoadBalancer:", "  RelayTargetGroup:")
        service = section(relay, "  RelayService:", "  RelayVpcOrigin:")
        listener = section(relay, "  RelayListener:", "  RelayTaskDefinition:")
        distribution = section(relay, "  VideoRelayDistribution:", "Outputs:")

        self.assertIn("Scheme: internal", alb)
        self.assertIn("Subnets: !Ref PrivateSubnetIds", alb)
        self.assertIn("AssignPublicIp: DISABLED", service)
        self.assertIn("Type: AWS::CloudFront::VpcOrigin", relay)
        self.assertIn("VpcOriginConfig:", distribution)
        self.assertIn("TrustedKeyGroups:", distribution)
        self.assertIn("ViewerProtocolPolicy: redirect-to-https", distribution)
        self.assertIn('StatusCode: "403"', listener)
        self.assertIn("X-Pairputer-Origin-Secret", listener)
        self.assertNotIn("AWS::Lambda::Url", relay)
        self.assertNotIn("internet-facing", alb)

    def test_session_secret_is_strong_and_verification_fails_closed(self):
        security = read("substrate/cloudformation/nested/security.yaml")
        relay = read("substrate/stateful-relay/index.mjs")
        server = read("substrate/mcp-server/server.py")
        generated = section(security, "  RelaySessionSecret:", "  RelayOriginHeaderSecret:")
        get_secret = section(relay, "async function getSecret()", "async function verifySessionToken")
        verify = section(relay, "async function verifySessionToken", "function hasChannel")
        mcp_secret = section(server, "def _get_session_secret", "def _get_cf_private_key")

        self.assertIn("PasswordLength: 48", generated)
        self.assertIn("const MIN_SESSION_SECRET_BYTES = 32", relay)
        self.assertIn("Buffer.byteLength(value, \"utf8\") < MIN_SESSION_SECRET_BYTES", get_secret)
        self.assertIn("relay session secret is missing or too short", get_secret)
        self.assertIn("const unsignedDev = ALLOW_UNSIGNED_DEV && !SESSION_SECRET_ARN", verify)
        self.assertIn("if (!unsignedDev)", verify)
        self.assertIn('crypto.createHmac("sha256", s)', verify)
        self.assertIn("crypto.timingSafeEqual(got, want)", verify)
        self.assertNotIn("if (s)", verify)
        self.assertIn("_MIN_SESSION_SECRET_BYTES = 32", server)
        self.assertIn("if LOCAL_MODE", mcp_secret)
        self.assertIn("production relay session secret ARN is not configured", mcp_secret)
        self.assertIn("relay session secret is missing or too short", mcp_secret)

    def test_relay_authorization_is_bound_to_durable_tenant_session(self):
        relay = read("substrate/stateful-relay/index.mjs")
        lookup = section(relay, "async function loadActiveSession", "function sessionClaimsFresh")
        fresh = section(relay, "function sessionClaimsFresh", "function json")
        authorize = section(relay, "async function authorize", "async function handleHttp")

        self.assertIn('pk: { S: `TENANT#${claims.tenantId}` }', lookup)
        self.assertIn('sk: { S: `IMAGE#${claims.imageId}` }', lookup)
        self.assertIn("TransactGetItemsCommand", lookup)
        self.assertIn('pk: { S: `MICROVM#${claims.microvmId}` }', lookup)
        self.assertIn('sk: { S: "OWNER" }', lookup)
        for binding in (
            "tenant_id) === claims.tenantId",
            "image_id) === claims.imageId",
            "microvm_id) === claims.microvmId",
            "session_id) === claims.sessionId",
            "currentSessionVersion === claims.sessionVersion",
        ):
            self.assertIn(binding, fresh)
        self.assertIn("owner.tenant_id", lookup)
        self.assertIn("owner.session_id", lookup)
        self.assertIn("owner.release_digest", lookup)
        self.assertIn("owner.image_version", lookup)
        self.assertIn("hasChannel(claims, channel)", authorize)
        self.assertIn("loadActiveSessionCoalesced(claims)", authorize)
        self.assertIn("sessionClaimsFresh(claims, current)", authorize)

    def test_mcp_mints_only_short_lived_tenant_and_session_bound_capabilities(self):
        server = read("substrate/mcp-server/server.py")
        caller = section(server, "def _caller_identity", "SESSION_TOKEN_TTL_SECONDS")
        mint = section(server, "def _mint_session_token", "def _cloudfront_b64")

        self.assertIn('if not auth.lower().startswith("bearer ")', caller)
        self.assertIn('if not issuer or not subject:', caller)
        self.assertIn('hashlib.sha256(f"{issuer}:{subject}"', caller)
        self.assertIn("SESSION_TOKEN_TTL_SECONDS = 15 * 60", server)
        for claim in ("tenantId", "sessionId", "sessionVersion", "microvmId", "imageId",
                      "releaseDigest", "manifestDigest", "imageArn", "imageVersion", "exp", "channels"):
            self.assertIn(f'"{claim}"', mint)
        self.assertIn('hmac.new(secret, payload_b64.encode(), hashlib.sha256)', mint)
        self.assertIn('"token": _relay_token(identity, vm, exp=exp)', server)
        self.assertIn('"edgeAuth": _cloudfront_signed_params(exp)', server)

    def test_vm_proxy_credential_never_enters_browser_artifacts(self):
        relay = read("substrate/stateful-relay/index.mjs")
        server = read("substrate/mcp-server/server.py")
        widget = read("substrate/mcp-server/app.html")

        self.assertIn("CreateMicrovmAuthTokenCommand", relay)
        self.assertIn('t.authToken["X-aws-proxy-auth"]', relay)
        self.assertIn('t["authToken"]["X-aws-proxy-auth"]', server)
        self.assertNotIn("X-aws-proxy-auth", widget)
        self.assertNotIn("authToken", widget)

    def test_mcp_and_run_hook_share_a_distinct_per_microvm_bridge_capability(self):
        server = read("substrate/mcp-server/server.py")
        readiness = read("capsules/computer-use-desktop/rootfs/opt/capsule/readiness.py")
        bridge = read("capsules/computer-use-desktop/rootfs/opt/capsule/agent_bridge.py")
        launch = section(server, "def _ensure_running", "def _vm_state")
        transport = section(server, "def _bridge(", "def _capsule_lifecycle_hook")
        public_payload = section(server, "def _session_payload", "def _play")
        self.assertIn("secrets.token_urlsafe(32)", launch)
        self.assertIn('"runHookPayload"', launch)
        self.assertIn('"bridge_capability": bridge_capability', launch)
        self.assertIn('"X-Pairputer-Bridge-Capability": bridge_capability', transport)
        self.assertNotIn("bridge_capability", public_payload)
        self.assertIn("accept_run_hook", readiness)
        self.assertIn("hmac.compare_digest", bridge)

    def test_session_store_enforces_unique_vm_owner_and_monotonic_cas(self):
        server = read("substrate/mcp-server/server.py")
        bind = section(server, "def _bind_new_vm_owner", "def _delete_vm_owner")
        save = section(server, "def _save_session", "def _ddb_map")
        launch = section(server, "def _ensure_running", "def _vm_state")
        self.assertIn('"pk": f"MICROVM#{microvm_id}"', bind)
        self.assertIn('"sk": "OWNER"', bind)
        self.assertIn("transact_write_items", bind)
        self.assertIn('"ConditionExpression": "attribute_not_exists(pk)"', bind)
        self.assertIn("record_version", save)
        self.assertNotIn('Attr("updated_at").eq', save)
        self.assertIn("launch_client_token", launch)
        self.assertIn("launch_bridge_capability", launch)
        self.assertIn('"clientToken": launch_client_token', launch)
        self.assertIn("_SESSION_RELEASE_FIELDS", bind)
        for field in ("release_digest", "manifest_digest", "image_arn", "image_version"):
            self.assertIn(field, server)
        rotate = section(server, "def _rotate_bound_session_epoch", "def _delete_vm_owner")
        self.assertIn("transact_write_items", rotate)
        self.assertIn("session_id = :session AND session_version = :version", rotate)
        freeze = section(server, "def freeze", "def thaw")
        self.assertIn("_rotate_bound_session_epoch(item)", freeze)
        self.assertLess(freeze.index("_rotate_bound_session_epoch(item)"),
                        freeze.index("_save_session(item)"))
        self.assertLess(freeze.index("_rotate_bound_session_epoch(item)"),
                        freeze.index("mvm.suspend_microvm"))

    def test_launch_and_relay_are_bound_to_one_immutable_release(self):
        server = read("substrate/mcp-server/server.py")
        launch = section(server, "def _ensure_running", "def _vm_state")
        self.assertIn('"imageVersion": image_version', launch)
        self.assertIn("_apply_release_binding(item, release)", launch)
        self.assertIn("launched MicroVM does not match", launch)
        self.assertIn("pairputer:capsule-release-ssm", server)
        self.assertIn("release digest mismatch", server)
        relay = read("substrate/stateful-relay/index.mjs")
        require_running = section(relay, "async function requireRunning", "async function getUpstreamToken")
        self.assertIn("vm.imageArn !== claims.imageArn", require_running)
        self.assertIn("vm.imageVersion !== claims.imageVersion", require_running)


if __name__ == "__main__":
    unittest.main()
