from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def section(text, start_marker, end_marker):
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


class MicrovmIamScopeTests(unittest.TestCase):
    def test_agentcore_microvm_lifecycle_is_scoped_to_capsule_images(self):
        agentcore = read_text("substrate/cloudformation/nested/agentcore.yaml")
        policy = section(agentcore, "        - PolicyName: PairputerMicrovmControl", "        - PolicyName: PairputerSessionSecret")
        control = section(policy, "              - Sid: MicrovmControl", "              - Sid: PassNetworkConnector")

        # MicroVM lifecycle control is scoped two least-privilege ways, NEVER unconditional "*":
        #   1. the bundled capsule image ARN(s) list, and
        #   2. any image carrying the pairputer:capsule=true tag (the cartridge model — capsules deployed
        #      as their own stacks later). The wildcard image ARN is present ONLY with that tag condition.
        self.assertIn("Resource: !Ref CapsuleImageArns", control)
        self.assertIn("aws:ResourceTag/pairputer:capsule", control)
        # The only wildcard MicroVM resource must be tag-conditioned (no unconditional microvm-image:*).
        self.assertNotIn('Resource: "arn:aws:lambda:${AWS::Region}:${AWS::AccountId}:microvm-image:*"\n              - Sid', control)
        self.assertNotIn("lambda:ListMicrovms", control)
        for action in (
            "lambda:RunMicrovm",
            "lambda:GetMicrovm",
            "lambda:SuspendMicrovm",
            "lambda:ResumeMicrovm",
            "lambda:TerminateMicrovm",
        ):
            self.assertIn(action, control)

    def test_agentcore_pass_network_connector_is_limited_to_configured_connectors(self):
        agentcore = read_text("substrate/cloudformation/nested/agentcore.yaml")
        policy = section(agentcore, "        - PolicyName: PairputerMicrovmControl", "        - PolicyName: PairputerSessionSecret")
        pass_connector = policy[policy.index("              - Sid: PassNetworkConnector"):]

        self.assertIn("Action: lambda:PassNetworkConnector", pass_connector)
        self.assertIn("aws-network-connector:ALL_INGRESS", pass_connector)
        self.assertIn("aws-network-connector:INTERNET_EGRESS", pass_connector)
        self.assertNotIn("aws-network-connector:*", pass_connector)
        self.assertNotIn('Resource: "*"', pass_connector)

    def test_relay_microvm_token_access_is_scoped_to_capsule_images(self):
        relay = read_text("substrate/cloudformation/nested/relay.yaml")
        self.assertIn("CapsuleImageArns:", relay)

        policy = section(relay, "        - PolicyName: RelayMicrovmAccess", "  RelayLogGroup:")
        microvm_statement = section(policy, "              - Effect: Allow", "              - Effect: Allow\n                Action: secretsmanager:GetSecretValue")

        self.assertIn("lambda:GetMicrovm", microvm_statement)
        self.assertIn("lambda:CreateMicrovmAuthToken", microvm_statement)
        self.assertIn("Resource: !Ref CapsuleImageArns", microvm_statement)
        self.assertNotIn('Resource: "*"', microvm_statement)

    def test_relay_can_reach_cartridge_capsules_by_tag(self):
        # Cartridges deploy as their own stacks AFTER the substrate, so their ARNs aren't in
        # CapsuleImageArns. The relay (video/state/input + log shipping) must reach them via the same
        # tag condition the MCP controller uses — else /coplay, video, and log shipping 502 for every
        # cartridge. The only wildcard MicroVM resource must be tag-conditioned.
        relay = read_text("substrate/cloudformation/nested/relay.yaml")
        policy = section(relay, "        - PolicyName: RelayMicrovmAccess", "  RelayLogGroup:")
        self.assertIn("RelayTaggedCapsules", policy)
        self.assertIn("aws:ResourceTag/pairputer:capsule", policy)
        self.assertIn("microvm-image:*", policy)  # wildcard ONLY under the tag condition

    def test_root_stack_passes_capsule_registry_and_arns(self):
        root = read_text("substrate/cloudformation/pairputer.yaml")
        relay_stack = section(root, "  RelayStack:", "  AgentCoreStack:")
        agentcore_stack = section(root, "  AgentCoreStack:", "Outputs:")

        # Relay gets the capsule ARN list (resolved from override or the built image), never "*".
        self.assertIn("CapsuleImageArns: !If", relay_stack)
        self.assertIn("UseDoomImageOverride", relay_stack)
        self.assertIn("DoomImageStack.Outputs.DoomImageArn", relay_stack)

        # AgentCore gets the registry JSON (id->{arn,name,description}) AND the ARN list AND the default anchor.
        self.assertIn("CapsuleRegistryJson: !If", agentcore_stack)
        self.assertIn('"${I}":{"arn":"${A}","name":"${N}","description":"${D}"}', agentcore_stack)
        self.assertIn("I: !Ref ReferenceCapsuleId", agentcore_stack)
        self.assertIn("CapsuleImageArns: !If", agentcore_stack)
        self.assertIn("DoomImageArn: !If", agentcore_stack)

    def test_registry_and_iam_are_n_capsule_capable(self):
        # The substrate must accept N capsules by design (ship 1 now). Registry is a JSON MAP the server
        # reads as PAIRPUTER_IMAGE_REGISTRY, and IAM scoping uses a CommaDelimitedList — both extend to
        # more capsules by adding entries, not by editing IAM shape or the server.
        agentcore = read_text("substrate/cloudformation/nested/agentcore.yaml")
        relay = read_text("substrate/cloudformation/nested/relay.yaml")

        # registry env is fed from the param (a JSON id->ARN map), not a hardcoded single entry.
        self.assertIn("PAIRPUTER_IMAGE_REGISTRY: !Ref CapsuleRegistryJson", agentcore)
        self.assertNotIn('PAIRPUTER_IMAGE_REGISTRY: !Sub', agentcore)  # no more hardcoded {"doom":...}

        # IAM scoping is a list param in both places.
        for tmpl in (agentcore, relay):
            self.assertIn("CapsuleImageArns:", tmpl)
            self.assertIn("Type: CommaDelimitedList", tmpl)
            self.assertIn("Resource: !Ref CapsuleImageArns", tmpl)


if __name__ == "__main__":
    unittest.main()
