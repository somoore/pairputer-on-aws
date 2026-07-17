from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def section(text, start_marker, end_marker):
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


class RelayOriginPrivateHopTests(unittest.TestCase):
    """Finding #1 remediation: the CloudFront->origin hop must not cross the public internet in
    cleartext. We close it with a CloudFront VPC origin fronting an INTERNAL ALB (private connection),
    which needs no ACM cert or Route53 zone. These tests assert that shape and that the old public
    HTTPS/ACM/Route53 approach is gone."""

    def test_alb_is_internal_in_private_subnets(self):
        relay = read_text("substrate/cloudformation/nested/relay.yaml")
        alb = section(relay, "  RelayLoadBalancer:", "  RelayTargetGroup:")
        self.assertIn("Scheme: internal", alb)
        self.assertIn("Subnets: !Ref PrivateSubnetIds", alb)
        self.assertNotIn("Scheme: internet-facing", alb)

    def test_cloudfront_uses_vpc_origin_not_public_custom_origin(self):
        relay = read_text("substrate/cloudformation/nested/relay.yaml")
        # The VPC origin resource exists and points at the ALB.
        self.assertIn("Type: AWS::CloudFront::VpcOrigin", relay)
        self.assertIn("Arn: !Ref RelayLoadBalancer", relay)

        distribution = section(relay, "  VideoRelayDistribution:", "Outputs:")
        self.assertIn("VpcOriginConfig:", distribution)
        self.assertIn("VpcOriginId: !GetAtt RelayVpcOrigin.Id", distribution)
        # No public custom origin / TLS-to-origin config remains.
        self.assertNotIn("CustomOriginConfig:", distribution)
        self.assertNotIn("OriginProtocolPolicy: https-only", distribution)

    def test_no_acm_certificate_or_route53_record(self):
        relay = read_text("substrate/cloudformation/nested/relay.yaml")
        self.assertNotIn("AWS::CertificateManager::Certificate", relay)
        self.assertNotIn("AWS::Route53::RecordSet", relay)
        self.assertNotIn("RelayOriginDomainName", relay)
        self.assertNotIn("RelayOriginHostedZoneId", relay)

    def test_listener_is_plain_http_inside_the_vpc(self):
        relay = read_text("substrate/cloudformation/nested/relay.yaml")
        listener = section(relay, "  RelayListener:", "  RelayListenerRule:")
        # HTTP:80 is acceptable because the whole hop is private (VPC origin), not public.
        self.assertIn("Port: 80", listener)
        self.assertIn("Protocol: HTTP", listener)
        self.assertNotIn("Protocol: HTTPS", listener)
        # The origin-secret header rule still gates forwarding (defense in depth).
        rule = section(relay, "  RelayListenerRule:", "  RelayTaskDefinition:")
        self.assertIn("X-Pairputer-Origin-Secret", rule)

    def test_fargate_tasks_have_no_public_ip(self):
        relay = read_text("substrate/cloudformation/nested/relay.yaml")
        service = section(relay, "  RelayService:", "  RelayVpcOrigin:")
        self.assertIn("AssignPublicIp: DISABLED", service)
        self.assertIn("Subnets: !Ref PrivateSubnetIds", service)

    def test_relay_has_no_public_origin_or_function_url_bypass(self):
        relay = read_text("substrate/cloudformation/nested/relay.yaml")
        listener = section(relay, "  RelayListener:", "  RelayListenerRule:")
        distribution = section(relay, "  VideoRelayDistribution:", "Outputs:")

        # The current stateful topology is stronger than a public Lambda URL with OAC: there is no
        # Function URL at all, the origin is an internal ALB, and its default action fails closed.
        self.assertNotIn("AWS::Lambda::Url", relay)
        self.assertNotIn("AWS::Lambda::FunctionUrl", relay)
        self.assertIn('StatusCode: "403"', listener)
        self.assertIn("TrustedKeyGroups:", distribution)
        self.assertIn("CloudFrontKeyGroupId", distribution)
        self.assertIn("X-Pairputer-Origin-Secret", distribution)

    def test_networking_stack_offers_three_modes_with_nat_choice(self):
        net = read_text("substrate/cloudformation/nested/relay-network.yaml")
        for mode in ("ExistingVpc", "CreateVpcFckNat", "CreateVpcNatGateway"):
            self.assertIn(mode, net)
        # fck-nat egress and managed NAT gateway are both present, conditionally.
        self.assertIn("Type: AWS::EC2::NatGateway", net)
        self.assertIn("t4g.nano", net)
        self.assertIn("SourceDestCheck: false", net)

    def test_root_stack_and_deploy_script_wire_networking_mode(self):
        root = read_text("substrate/cloudformation/pairputer.yaml")
        self.assertIn("NetworkingMode:", root)
        relay_net = section(root, "  RelayNetworkStack:", "  RelayStack:")
        self.assertIn("NetworkingMode: !Ref NetworkingMode", relay_net)
        relay_stack = section(root, "  RelayStack:", "  AgentCoreStack:")
        self.assertIn("PrivateSubnetIds: !GetAtt RelayNetworkStack.Outputs.ResolvedPrivateSubnetIds", relay_stack)

        deploy = read_text("substrate/deploy.sh")
        self.assertIn("PAIRPUTER_NETWORKING_MODE", deploy)
        self.assertIn("NetworkingMode=${NETWORKING_MODE}", deploy)
        # The old Route53/ACM env contract must be gone.
        self.assertNotIn("PAIRPUTER_RELAY_ORIGIN_HOSTED_ZONE_ID", deploy)


if __name__ == "__main__":
    unittest.main()
