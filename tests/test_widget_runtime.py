"""Execute the shipped widget/player JavaScript with deterministic host/media fakes.

These checks do not certify ChatGPT CSP, autoplay policy, or audible output.
Run directly with: node --test tests/widget_runtime.cjs
"""
from pathlib import Path
import ast
import subprocess
import unittest


class WidgetRuntimeTests(unittest.TestCase):
    def test_resource_csp_dialects_allow_only_the_configured_relay(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "substrate/mcp-server/server.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_resource_meta")
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        for relay in ("", "https://relay.example.test"):
            with self.subTest(relay=relay):
                namespace = {"VIDEO_RELAY_URL": relay}
                exec(compile(module, "server.py:_resource_meta", "exec"), namespace)
                meta = namespace["_resource_meta"]()
                domains = [relay] if relay else []
                self.assertEqual(meta["ui"]["csp"], {
                    "connectDomains": domains, "resourceDomains": domains, "frameDomains": domains,
                })
                self.assertEqual(meta["openai/widgetCSP"], {
                    "connect_domains": domains, "resource_domains": domains,
                    "frame_domains": domains, "redirect_domains": domains,
                })

    def test_widget_and_player_runtime(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "--test", "tests/widget_runtime.cjs"], cwd=root,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
