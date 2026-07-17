"""End-to-end cartridge contract and integration invariants."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CAPSULE = ROOT / "capsules/computer-use-desktop"
MANIFEST = yaml.safe_load((CAPSULE / "capsule.yaml").read_text())["capsule"]
BRIDGE = (CAPSULE / "rootfs/opt/capsule/agent_bridge.py").read_text()
INPUT = (CAPSULE / "rootfs/opt/capsule/input_ws.py").read_text()
START = (CAPSULE / "rootfs/opt/capsule/start.sh").read_text()
DOCKERFILE = (CAPSULE / "Dockerfile").read_text()
CHROMIUM = (CAPSULE / "rootfs/usr/local/bin/pairputer-chromium").read_text()
ADAPTERS = (CAPSULE / "rootfs/opt/capsule/desktopd_adapters.py").read_text()
SERVER = (ROOT / "substrate/mcp-server/server.py").read_text()


def test_manifest_is_independent_secure_capsule():
    assert (CAPSULE / ".dockerignore").is_file()
    assert MANIFEST["id"] == "computer-use-desktop"
    assert MANIFEST["name"] == "Pairputer Workbench"
    assert MANIFEST["permissions"]["iamRole"] == "none"
    # Autonomy posture: the VM is disposable, so the agent may act by default. The
    # authoritative gate is policy.py (PAIRPUTER_WORKBENCH_AUTONOMY) — in-VM effects run
    # free, external-world commits still require approval.
    assert MANIFEST["interaction"] == {"tier1": False, "agentInteractDefault": True}
    assert MANIFEST["bridge"] == {"port": 6905, "protocol": "http-json"}
    assert MANIFEST["runtime"]["minimumMemoryMiB"] == 8192
    assert MANIFEST["lifecycle"] == {
        "beforeFreeze": "/lifecycle/pre-freeze",
        "afterThaw": "/lifecycle/post-thaw",
    }


def test_all_declared_tools_are_typed_unique_and_routed():
    tools = MANIFEST["tools"]
    names = [tool["name"] for tool in tools]
    assert len(names) == len(set(names)) == 33
    required = {
        "observe", "screenshot", "physical_input", "computer_action", "ground_target", "drive_task", "continue_task", "task_status", "cancel_task", "approve_action",
        "workspace_list", "workspace_describe", "workspace_read", "workspace_write", "workspace_mkdir", "workspace_upload",
        "workspace_patch", "workspace_move", "workspace_trash", "run_command", "job_status",
        "cancel_job", "list_apps", "open_app", "list_windows", "focus_window", "ui_tree",
        "ui_action", "browser_open", "browser_observe", "browser_query", "browser_action", "export_artifact",
    }
    assert set(names) == required
    # Front-of-house contract: only this slim core is advertised in tools/list (per-turn context
    # cost); everything else carries advertise: false and stays fully callable via capsule_invoke
    # + discoverable via capsule_metadata. Changing this set is a product decision, not a tweak.
    advertised = {tool["name"] for tool in tools if tool.get("advertise") is not False}
    assert advertised == {
        "observe", "screenshot", "computer_action", "ground_target", "drive_task", "task_status",
        "run_command", "workspace_read", "workspace_write", "workspace_upload",
        "browser_open", "browser_query",
    }
    for tool in tools:
        assert tool["path"] in BRIDGE
        assert tool["inputSchema"]["type"] == "object"
        assert isinstance(tool["requiresApproval"], bool)
        assert tool["riskClass"]
    by_name = {tool["name"]: tool for tool in tools}
    # drive_task no longer host-gated: starting a task is local_reversible and the human can
    # pause/trash anytime; policy.py still gates any external commit the task attempts.
    assert by_name["drive_task"]["requiresApproval"] is False
    # approve_action and physical_input remain gated as the real-world / exact-consent surface.
    assert by_name["approve_action"]["requiresApproval"] is True
    # computer_action is the OPEN CUA surface: no approval, no proof/epoch inputs, drop-in for
    # a stock OpenAI/Anthropic computer-use loop. The human-first arbiter still preempts it.
    ca = by_name["computer_action"]
    assert ca["requiresApproval"] is False
    assert ca["path"] == "/computer/action"
    assert "target_proof" not in (ca["inputSchema"].get("required") or [])
    assert "smallest allowed_capabilities" in by_name["drive_task"]["description"]
    physical = by_name["physical_input"]
    assert physical["path"] == "/input"
    assert physical["requiresApproval"] is True
    assert set(physical["inputSchema"]["required"]) == {
        "events", "target_proof", "expected_human_epoch", "expected_world_revision",
    }
    target = physical["inputSchema"]["properties"]["target_proof"]
    assert "focused_window" in target["required"]
    assert physical["inputSchema"]["properties"]["events"]["maxItems"] == 32
    assert physical["approvalPolicy"] == "exact_action_single_use"
    assert physical["riskClass"] == "external_commit"
    assert physical["idempotency"] == "never_blindly_retry"
    assert by_name["screenshot"]["path"] == "/screenshot"
    assert by_name["screenshot"]["riskClass"] == "read_only"
    assert by_name["run_command"]["requiresApproval"] is True
    assert by_name["run_command"]["riskClass"] == "local_destructive"
    assert by_name["ui_action"]["requiresApproval"] is True
    patch_schema = by_name["workspace_patch"]["inputSchema"]
    assert {"expected_sha256", "hunks"} <= set(patch_schema["required"])
    assert "content" not in patch_schema["properties"]
    assert by_name["workspace_write"]["inputSchema"]["properties"]["encoding"]["enum"] == ["utf-8", "base64"]
    upload_schema = by_name["workspace_upload"]["inputSchema"]
    # 'final' is now OPTIONAL: the upload auto-commits when staged bytes reach total_size, so a
    # caller need not pass it. The integrity fields remain required.
    assert {"upload_id", "offset", "chunk_sha256", "total_size", "total_sha256"} <= set(upload_schema["required"])
    assert "final" not in upload_schema["required"]
    assert "final" in upload_schema["properties"]
    app_schema = by_name["open_app"]["inputSchema"]
    assert app_schema["properties"]["app_id"]["enum"] == ["browser", "editor", "terminal", "files"]
    assert "app_name" not in app_schema["properties"]
    export_schema = by_name["export_artifact"]["inputSchema"]
    assert {"expected_sha256", "action_id", "idempotency_key"} <= set(export_schema["required"])
    assert "allowed_domains" not in by_name["browser_open"]["inputSchema"]["properties"]
    assert by_name["browser_query"]["requiresApproval"] is False
    assert by_name["browser_query"]["riskClass"] == "read_only"


def test_runtime_log_shipping_uses_real_bounded_debug_tails():
    readiness = (CAPSULE / "rootfs/opt/capsule/readiness.py").read_text()
    startup = (CAPSULE / "rootfs/opt/capsule/start.sh").read_text()
    bounded_log = CAPSULE / "rootfs/opt/capsule/bounded_log.py"
    assert bounded_log.is_file()
    assert '"/dbg/inputws":"/var/log/pairputer-input-ws.log"' in readiness
    assert '"/dbg/bridge":"/var/log/pairputer-agent-bridge.log"' in readiness
    assert "MAX_DEBUG_LOG_CHUNK=256*1024" in readiness
    assert "bounded_log.py /var/log/pairputer-input-ws.log" in startup
    assert "bounded_log.py /var/log/pairputer-agent-bridge.log" in startup


def test_task_brain_has_one_durable_loop_and_epoch_listener():
    for token in (
        "class _BrainLoop", "run_coroutine_threadsafe", "brain_api.submit_task",
        "brain_api.continue_task", "brain_api.before_freeze", "brain_api.after_thaw",
        "brain-preempt.sock", "synchronize_human_epoch", "desktop-brain-loop",
    ):
        assert token in BRIDGE
    assert "asyncio.run(brain_api" not in BRIDGE


def test_physical_input_forwards_only_the_approved_screenshot_epoch_and_revision():
    agent_input = BRIDGE[BRIDGE.index("def _agent_input("):BRIDGE.index("def _screen(")]
    assert "CONTROL.snapshot()" not in agent_input
    assert '"expected_human_epoch": expected_human_epoch' in agent_input
    assert '"display_revision": expected_world_revision' in agent_input
    route = BRIDGE[BRIDGE.index('if path == "/input":'):BRIDGE.index('if path == "/brain/drive":')]
    assert '"expected_human_epoch"' in route
    assert '"expected_world_revision"' in route
    screenshot = (CAPSULE / "rootfs/opt/capsule/services/screenshot_service.py").read_text()
    assert 'result["expectedHumanEpoch"] = current["humanEpoch"]' in screenshot
    assert 'result["expectedWorldRevision"] = current["worldRevision"]' in screenshot
    assert 'result["targetProof"]' in screenshot


def test_task_domain_grants_carry_identity_in_the_authenticated_envelope():
    assert '"task_domain_grant",\n            {"allowed_domains": list(allowed_domains)}, task_id=task_id' in ADAPTERS
    assert '"task_domain_revoke", {}, task_id=task_id' in ADAPTERS


def test_freeze_barrier_releases_input_and_substrate_invokes_hooks():
    assert "def freeze_barrier" in INPUT
    assert "releasedHeldInputs" in INPUT
    assert '"/lifecycle/pre-freeze"' in INPUT
    assert "_input_freeze_barrier()" in BRIDGE
    freeze = SERVER[SERVER.index("def freeze("):SERVER.index("def thaw(")]
    assert freeze.index("_capsule_lifecycle_hook(identity, image_id, \"beforeFreeze\")") < freeze.index("_drain_relay(identity, vm)")
    thaw = SERVER[SERVER.index("def thaw("):SERVER.index("def trash_microvm(")]
    assert thaw.index("payload = _play(identity, cid)") < thaw.index('"afterThaw"')


def test_browser_never_auto_opens_only_on_demand():
    # The browser must NOT auto-open at boot (Scott: only a human or a model tool call opens it).
    # Three invariants pin that: (1) session.sh has no boot Chromium launch, (2) readiness does not
    # GATE on chromium (informational only — a required check is what originally forced the boot
    # launch), (3) browser_open can START the browser when CDP is down (on-demand open must work on
    # a fresh box where nothing pre-launched it).
    session = (CAPSULE / "rootfs/opt/capsule/session.sh").read_text()
    # match the executable invocation, not prose — a comment may mention the launcher by name
    assert "/usr/local/bin/pairputer-chromium" not in session, "session.sh must not launch the browser at boot"
    readiness = (CAPSULE / "rootfs/opt/capsule/readiness.py").read_text()
    required = readiness[readiness.index("def checks"):readiness.index("def info_checks")]
    # match the dict KEYS, not prose — the section carries a comment explaining the decision
    assert '"chromium_cdp"' not in required and '"chromium_visible"' not in required, \
        "readiness must not gate on a running browser"
    info = readiness[readiness.index("def info_checks"):readiness.index("def monitor")]
    assert '"chromium_cdp"' in info and '"chromium_visible"' in info  # still visible as signals
    browser = (CAPSULE / "rootfs/opt/capsule/services/browser_service.py").read_text()
    assert "def _ensure_browser" in browser
    open_body = browser[browser.index("def open(self, request):"):browser.index("def query(") if "def query(" in browser else len(browser)]
    assert "self._ensure_browser()" in open_body, "browser_open must launch the browser on demand"
    apps = (CAPSULE / "rootfs/opt/capsule/services/app_service.py").read_text()
    assert "def browser_launch_argv" in apps  # the shared on-demand launch argv


def test_browser_and_private_control_planes_fail_closed():
    assert "150.0.7871.100-1" in DOCKERFILE
    assert "26e2e66c8c5d94bb02c98d028b03cd80a4dace9284710c984efd7188e78a61b4" in DOCKERFILE
    assert "9fba404e26dfd6ec0c8fd6d16d04113caad7a49832e1d60c0e87c74afdc352d5" in DOCKERFILE
    assert "GH_CLI_VERSION=2.94.0" in DOCKERFILE
    assert "705a23b70b0f1b7ba4c302fdcef392ce3edaacfa7ce8e85e4d93d72ea800a538" in DOCKERFILE
    assert "RIPGREP_VERSION=15.1.0" in DOCKERFILE
    assert "2b661c6ef508e902f388e9098d9c4c5aca72c87b55922d94abdba830b4dc885e" in DOCKERFILE
    assert "GITHUB_TOKEN" not in DOCKERFILE
    assert 'PAIRPUTER_ALLOW_LOCAL_PREVIEW=true' in DOCKERFILE
    assert 'PAIRPUTER_PREVIEW_PORTS="3000-5899,7000-8999"' in DOCKERFILE
    # Local app previews are first-class, but every capsule control/media port
    # stays outside the browser-reachable preview ranges.
    for protected_port in (5901, 6901, 6902, 6903, 6904, 6905, 6906, 6907, 9000, 9222, 50051):
        assert not (3000 <= protected_port <= 5899 or 7000 <= protected_port <= 8999)
    assert "chrome-sandbox" in DOCKERFILE and "-m 4755" in DOCKERFILE
    seccomp = (ROOT / "substrate/generate-seccomp-profile.py").read_text()
    assert "moby/profiles/refs/tags/seccomp/v0.2.1" in seccomp
    assert '"clone", "clone3", "unshare"' in seccomp
    assert "seccomp=unconfined" not in seccomp
    assert "--no-sandbox" not in DOCKERFILE + CHROMIUM
    assert "--disable-setuid-sandbox" not in CHROMIUM
    assert "--remote-debugging-address=127.0.0.1" in CHROMIUM
    assert "--remote-allow-origins=http://127.0.0.1" in CHROMIUM
    assert '--proxy-server="http://${proxy_host}:${proxy_port}"' in CHROMIUM
    # The bypass list is EXACTLY the loopback code-server port, nothing else. code-server (VS Code) is a
    # same-box internal service; the egress proxy hard-rejects raw loopback GETs and cleartext
    # `Upgrade: websocket` (egress_proxy.py), so routing 127.0.0.1:4500 through it kills the workbench
    # WebSocket with a 1006 the instant it loads. A `<-loopback>` blanket (which forced ALL loopback
    # through the proxy) must NOT come back — only this one host:port may bypass, so no other in-box port
    # is exposed while the egress boundary still governs every outbound-internet destination.
    assert '--proxy-bypass-list="127.0.0.1:${code_server_port}"' in CHROMIUM
    assert '<-loopback>' not in CHROMIUM
    assert 'code_server_port="${PAIRPUTER_CODE_SERVER_PORT:-4500}"' in CHROMIUM
    assert "--disable-quic" in CHROMIUM
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in CHROMIUM
    # No --host-resolver-rules: it triggered Chromium's "unsupported flag" banner for no gain — the
    # mandatory --proxy-server already resolves all names proxy-side (Chromium never resolves locally
    # when a fixed proxy is set), so DNS/egress containment rides the proxy, not a resolver rule. The
    # flag must NOT come back as an argument; the fixed-proxy + bypass-loopback combo (above) is the
    # control. (A comment mentioning the removed flag is fine — only a passed `--host-resolver-rules=`
    # argument is forbidden.)
    assert "--host-resolver-rules=" not in CHROMIUM
    assert "PAIRPUTER_EGRESS_PROXY_PORT=6907" in DOCKERFILE
    assert "useradd -M -N -u 1004" in DOCKERFILE and "egressd" in DOCKERFILE
    assert "groupadd -r egressd" in DOCKERFILE and "-g egressd egressd" in DOCKERFILE
    assert "set -euo pipefail" in START
    assert "root -g egressd -m 0750 /run/pairputer/preview-grants" in START
    proxy = (CAPSULE / "rootfs/opt/capsule/egress_proxy.py").read_text()
    assert "PROTECTED_PORTS" in proxy and "MAX_CONNECTION_LIFETIME" in proxy
    assert "target.sockaddr" in proxy and "socket.getaddrinfo" in proxy
    assert "request targets, URLs, or headers" in proxy
    assert "SingletonCookie SingletonLock SingletonSocket" in CHROMIUM
    assert "pgrep -u" in CHROMIUM and "rm -f --" in CHROMIUM
    assert 'address = "127.0.0.1:50051"' in (CAPSULE / "rootfs/opt/capsule/desktopd.py").read_text()
    assert "seccomp=unconfined" not in (ROOT / "substrate/local-dev.sh").read_text()
    local_dev = (ROOT / "substrate/local-dev.sh").read_text()
    assert "127.0.0.1:6904:6904" in local_dev and "127.0.0.1:9000:9000" in local_dev
    assert "5901:5901" not in local_dev  # unauthenticated RFB stays inside the capsule network namespace
    assert 'self.headers.get("Origin")' in BRIDGE
    assert 'content_type != "application/json"' in BRIDGE
    assert "runuser -u agent" in START and "python3.11 /opt/capsule/agent_bridge.py" in START
    for name in ("audio_ws.py", "video_ws.py", "input_ws.py"):
        media = (CAPSULE / "rootfs/opt/capsule" / name).read_text()
        assert "Origin" in media
        assert "max_size=None" not in media
    accessibility = (CAPSULE / "rootfs/opt/capsule/services/accessibility_service.py").read_text()
    atspi = (CAPSULE / "rootfs/opt/capsule/observers/atspi.py").read_text()
    assert "if not app_name or app_name not in self.allowed_apps" in accessibility
    assert "visited >= self.max_nodes" in atspi
    windows = (CAPSULE / "rootfs/opt/capsule/observers/windows_x11.py").read_text()
    assert '"provenance": "untrusted_x11"' in windows


def test_runtime_starts_full_shared_desktop_contract():
    for port in (6901, 6902, 6903, 6904, 6905, 6906, 9000):
        assert str(port) in START or str(port) in DOCKERFILE
    for component in (
        "Xvnc", "websockify", "dbus-run-session", "video_ws", "audio_ws", "input_ws",
        "desktopd", "agent_bridge", "readiness.py",
    ):
        assert component in START
    assert "workbench_eval_runner.py" in DOCKERFILE and "eval-cases" in DOCKERFILE and "fixtures" in DOCKERFILE


def test_readiness_is_snapshotted_then_continuously_revalidated():
    readiness = (CAPSULE / "rootfs/opt/capsule/readiness.py").read_text()
    assert 'READY_FLAG="/run/capsule.ready"' in readiness
    assert "threading.Thread(target=monitor" in readiness
    assert 'STATE.update({"ready":ready,"checks":values' in readiness
    assert "os.unlink(READY_FLAG)" in readiness
    assert "BrokenPipeError" in readiness


def test_bridge_has_per_microvm_capability_and_job_plane_is_separate():
    bridge = (CAPSULE / "rootfs/opt/capsule/agent_bridge.py").read_text()
    readiness = (CAPSULE / "rootfs/opt/capsule/readiness.py").read_text()
    dockerfile = (CAPSULE / "Dockerfile").read_text()
    start = (CAPSULE / "rootfs/opt/capsule/start.sh").read_text()
    process = (CAPSULE / "rootfs/opt/capsule/services/process_service.py").read_text()
    adapters = (CAPSULE / "rootfs/opt/capsule/desktopd_adapters.py").read_text()
    assert "X-Pairputer-Bridge-Capability" in bridge and "hmac.compare_digest" in bridge
    assert "accept_run_hook" in readiness and "runHookPayload" in readiness
    assert "RUN_HOOK_ACCEPTED" in readiness and "RUN_HOOK_DISABLED" in readiness
    assert "run_hook_already_accepted" in readiness and "run_hook_disabled" in readiness
    assert "useradd -m -u 1005 -g app" in dockerfile and "iptables-nft" in dockerfile and "nftables" in dockerfile
    assert "-m cgroup --path" in start and "5901,6001,6901,6902,6903,6904,6905,6906,6907,9000,9222,50051" in start
    assert "PAIRPUTER_JOB_CGROUP_PATH" in start and "cgroup.procs" in process and "PAIRPUTER_ALLOW_UID_FIREWALL" in start
    assert "CLONE_NEWNS" in process or "clone_newns" in process
    assert "job-empty-x11" in process and 'pwd.getpwnam(cls.JOB_USER)' in process
    assert '{"focus": "focus", "click": "click", "fill": "fill"}' in adapters
    assert "pairputer-state agent" not in dockerfile
    assert MANIFEST["interaction"]["tier1"] is False
    tier1_gate = SERVER[SERVER.index("def _screen_tier1"):SERVER.index("# --- Tier 1")]
    assert 'get("tier1")' in tier1_gate and "does not permit raw Tier-1 input" in tier1_gate


def test_every_service_restart_loop_is_set_e_guarded():
    # Resilience invariant: start.sh runs each service in a `(while :; do ...; done)&` restart
    # loop under `set -euo pipefail`. If a service exits non-zero and its loop body is NOT wrapped
    # in `if ...; then rc=0; else rc=$?; fi`, set -e kills that restart loop instead of healing it.
    # PID 1 waits only for critical Xvnc: ordinary finite desktop children (especially the initial
    # xterm window) must be allowed to exit without terminating the whole MicroVM.
    start = (CAPSULE / "rootfs/opt/capsule/start.sh").read_text()
    assert "set -euo pipefail" in start
    assert not any(line.strip() == "wait -n" for line in start.splitlines())
    assert 'wait "$xvnc_pid" || true' in start
    assert 'log "critical Xvnc process exited"' in start
    import re
    # find each `(while :;do ... ;done)&` restart-loop block that launches a python service
    loops = re.findall(r"\(while :;\s*do(.*?);\s*done\)&", start, re.DOTALL)
    service_loops = [body for body in loops if "python3.11 /opt/capsule/" in body]
    assert len(service_loops) >= 4, f"expected the known service loops, found {len(service_loops)}"
    for body in service_loops:
        launches_service = re.search(r"python3\.11 /opt/capsule/\w+\.py", body)
        assert launches_service, body
        # the service invocation must be guarded so a non-zero exit can't trip set -e
        assert "then rc=0;else rc=$?" in body or "then rc=0; else rc=$?" in body, (
            "UNGUARDED service loop would crash the whole MicroVM on a service exit: " + body[:160])


def test_x11_cookie_is_limited_to_trusted_desktop_principals():
    dockerfile = (CAPSULE / "Dockerfile").read_text()
    start = (CAPSULE / "rootfs/opt/capsule/start.sh").read_text()
    session = (CAPSULE / "rootfs/opt/capsule/session.sh").read_text()
    process = (CAPSULE / "rootfs/opt/capsule/services/process_service.py").read_text()
    assert "xorg-x11-xauth" in dockerfile
    assert "groupadd -r pairputer-x11" in dockerfile
    for user in ("app", "terminal", "agent", "inputd"):
        line = next(line for line in dockerfile.splitlines() if "useradd" in line and user in line)
        assert "pairputer-x11" in line
    job_line = next(line for line in dockerfile.splitlines() if "useradd" in line and " job " in line)
    assert "pairputer-x11" not in job_line
    assert 'XAUTHORITY=/run/pairputer/xauthority' in start
    assert 'chown root:pairputer-x11 "$XAUTHORITY"' in start
    assert '-auth "$XAUTHORITY" -nolisten local' in start
    assert start.count('XAUTHORITY="$XAUTHORITY"') >= 5
    assert "/run/pairputer/xauthority" in session
    assert '"DISPLAY": "", "XAUTHORITY": "/dev/null"' in process


def test_bridge_presentation_hook_never_references_envelope_out_of_scope():
    """Regression: the visible-cursor _present_action hook must only run inside the ROUTES branch
    that defines `envelope`. A v7 image referenced envelope after the branch, 500-ing every
    read-only route (observe/list/read) with 'envelope not associated with a value'. Pin that the
    presentation call sits in the same block as the envelope assignment, and that read-only routes
    (which never build an envelope) can't reach it."""
    import ast
    src = (CAPSULE / "rootfs/opt/capsule/agent_bridge.py").read_text()
    tree = ast.parse(src)
    # Find _present_action call sites and envelope assignments; every call must be dominated by an
    # envelope assignment in the same enclosing function body (cheap structural proxy: same lineno
    # block — the call's line is greater than an envelope assignment and less than the next dedent).
    calls = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_present_action"]
    assigns = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") == "envelope" for t in n.targets)]
    assert calls, "expected a _present_action call in the bridge"
    for call_line in calls:
        prior = [a for a in assigns if a < call_line]
        assert prior, f"_present_action at line {call_line} runs before any envelope assignment"
        # the nearest envelope assignment must be within ~15 lines (same branch, not a distant one)
        assert call_line - max(prior) < 15, "_present_action is too far from its envelope assignment"


def test_every_bridge_result_branch_sends_a_response():
    """Regression: the envelope refactor deleted a shared 'send(result)' tail, orphaning the
    /observe and /capabilities branches — they set `result` but never sent, so the bridge closed
    the connection with no response (RemoteDisconnected -> 502 at the AWS proxy). Every branch that
    binds `result = _rpc(...)` must self._send within a few lines, or the route silently hangs."""
    src = BRIDGE.splitlines()
    result_lines = [i for i, ln in enumerate(src) if "result = _rpc(" in ln or "result = BRAIN" in ln]
    assert result_lines, "expected result = _rpc branches in the bridge"
    for i in result_lines:
        sent = False
        for ln in src[i + 1:i + 14]:
            stripped = ln.strip()
            if stripped.startswith(("elif ", "else:", "if path")):
                break
            if "self._send(" in stripped:
                sent = True
                break
        assert sent, (
            f"bridge line {i+1} sets result but reaches the next branch with no self._send — "
            "orphaned route (RemoteDisconnected -> 502):\n" + "\n".join(src[i:i + 6]))


AIRGAP = (CAPSULE / "rootfs/opt/capsule/airgap.sh").read_text()
RECONCILER = (CAPSULE / "rootfs/opt/capsule/airgap-reconciler.sh").read_text()


def test_dev_tools_are_pinned_and_installed():
    """Tier-1 dev toolchain: code-server + uv are sha256-pinned like the other third-party
    assets, and the QOL packages are in the dnf install."""
    for arg in ("UV_SHA256=", "CODE_SERVER_SHA256="):
        assert arg in DOCKERFILE, f"missing pinned {arg}"
    assert "install -o root -g root -m 0755 uv-aarch64-unknown-linux-gnu/uv /usr/local/bin/uv" in DOCKERFILE
    assert "/opt/code-server/bin/code-server" in DOCKERFILE
    # vim-enhanced not neovim: AL2023 has no neovim package (probed live 2026-07-12).
    for pkg in ("vim-enhanced", "htop", "tree", "unzip", "less", "bash-completion"):
        assert pkg in DOCKERFILE, f"{pkg} not in dnf install"


def test_airgap_reconciled_and_control_plane_survives():
    """Air-gap is enforced by a root reconciler reading the bridge's intent file. Default is ON
    (AWS-proven safe by a 15-min live soak). The LOAD-BEARING invariant: the enforcement rejects
    ONLY public destinations and RETURNs every private/link-local range, so the aws-proxy control
    plane is never touched — an over-broad reject-all once bricked the bridge unrecoverably."""
    assert 'PAIRPUTER_AIRGAP_DEFAULT="${PAIRPUTER_AIRGAP_DEFAULT:-on}"' in START
    assert "airgap-reconciler.sh" in START
    assert 'PAIRPUTER_AIRGAP_DEFAULT:-on' in RECONCILER
    assert "airgap.intent" in RECONCILER
    # enforcement: only PUBLIC destinations are rejected; loopback + every private/link-local range
    # is exempt so the aws-proxy control plane is never touched (the proven job-firewall shape).
    assert "PAIRPUTER-AIRGAP" in AIRGAP
    assert "-o lo -j RETURN" in AIRGAP
    assert "169.254.0.0/16" in AIRGAP  # link-local / cloud-metadata range exempt
    assert "10.0.0.0/8" in AIRGAP      # RFC1918 exempt (control plane lives here)
    assert 'EXEMPT_DESTS' in AIRGAP


def test_bridge_airgap_route_writes_intent_only():
    """The bridge expresses intent (unprivileged) and never shells out to iptables itself."""
    assert '"/network/airgap"' in BRIDGE
    assert "def _set_airgap" in BRIDGE
    assert "AIRGAP_INTENT_FILE" in BRIDGE
    # observe surfaces network posture so the widget reads it from one snapshot
    assert '"network"' in BRIDGE and "_airgap_snapshot" in BRIDGE
    # the bridge must NOT run the firewall directly — that's root's job. It may
    # mention iptables in a comment, but never invoke it or the airgap script.
    assert '"iptables"' not in BRIDGE and "airgap.sh" not in BRIDGE


def test_server_exposes_network_airgap_tool():
    assert 'name="network_airgap"' in SERVER
    assert "def network_airgap" in SERVER


def test_frictionless_write_path_for_dropping_code_in():
    # "Drop code into the sandbox" must be a first-class, low-ceremony method: workspace_write requires
    # only {path, content} (the anti-drift envelope is optional; the bridge fills current epoch/revision
    # when omitted, keeping the strict check opt-in). drive_task is steered AWAY from simple authoring.
    tools = {t["name"]: t for t in MANIFEST["tools"]}
    assert tools["workspace_write"]["inputSchema"]["required"] == ["path", "content"]
    assert tools["workspace_mkdir"]["inputSchema"]["required"] == ["path"]
    # bridge auto-fills the envelope for the write-family routes when the caller omits it
    assert "_AUTOFILL_ENVELOPE_ROUTES" in BRIDGE
    assert "def _autofill_write_envelope" in BRIDGE
    assert '"/workspace/write"' in BRIDGE and '"/workspace/upload"' in BRIDGE
    # "write a page then OPEN it" is frictionless too: browser_open/open_app auto-fill the envelope
    assert '"/browser/open"' in BRIDGE and '"/apps/open"' in BRIDGE
    # a passed envelope still gets the exact-consent check (only OMITTED fields are filled)
    assert '"expected_human_epoch" not in body' in BRIDGE
    # drive_task description steers simple authoring to workspace_write + browser_open
    assert "skip drive_task" in RUNTIME or "do NOT reach for it to simply create" in \
        next(t["description"] for t in MANIFEST["tools"] if t["name"] == "drive_task")
    # browser_open documents the confined file:// open path
    bo = next(t["description"] for t in MANIFEST["tools"] if t["name"] == "browser_open")
    assert "file:///home/app/workspace" in bo


def test_confined_file_scheme_in_browser_service():
    bsrc = (CAPSULE / "rootfs/opt/capsule/services/browser_service.py").read_text()
    assert "def _authorize_workspace_file" in bsrc
    assert "_WORKSPACE_ROOT" in bsrc
    assert "confined to the workspace" in bsrc
    # file:// is authorized BEFORE the domain-grant/SSRF machinery (it's domain-free + workspace-confined)
    assert 'if _pre.scheme == "file":' in bsrc


def test_jwt_defense_in_depth_verification_wired():
    """The container re-verifies the bearer JWT (signature/iss/exp) so the tenant model doesn't rely
    SOLELY on AgentCore being the only ingress — fail-closed, with a no-op only when unconfigured."""
    assert "_verify_jwt(token)" in SERVER               # called in _caller_identity before trusting claims
    assert "PAIRPUTER_JWT_DISCOVERY_URL" in SERVER        # the OIDC discovery/JWKS source
    assert 'raise PermissionError("JWT signature verification failed")' in SERVER
    assert 'raise PermissionError("JWT is expired")' in SERVER
    # graceful no-op ONLY when discovery URL unset (rely on AgentCore alone) or LOCAL_MODE
    assert "if not JWT_DISCOVERY_URL or LOCAL_MODE:" in SERVER


def test_vm_ownership_assertion_is_wired_into_every_vm_touch():
    """Defense-in-depth: every path that resolves/mutates the caller's VM re-checks the stored
    tenant_id == caller tenant_id and FAILS CLOSED on a mismatch. The session key is already
    TENANT#<caller> (structural isolation), but the switch flow (freeze/thaw/launch) is exactly where
    a future key-derivation bug would be catastrophic, so the assertion is belt-and-suspenders."""
    assert "def _assert_owns(" in SERVER
    assert "hmac.compare_digest(owner, identity.tenant_id)" in SERVER
    assert "refusing cross-tenant operation" in SERVER
    # wired into the read path (_discover_vm) and the run/resume path (_ensure_running); freeze/trash
    # go through _discover_vm, so they inherit the check.
    assert '_assert_owns(identity, item, "discover_vm")' in SERVER
    assert '_assert_owns(identity, item, "ensure_running")' in SERVER


def test_assert_owns_rejects_a_foreign_tenant_item():
    """Functional check: _assert_owns raises PermissionError when the item's tenant_id differs from
    the caller's, and passes when they match (or the item has no owner yet)."""
    import ast, hmac, types as _t, logging as _l
    tree = ast.parse(SERVER)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_assert_owns")
    mod = ast.Module(body=[fn], type_ignores=[]); ast.fix_missing_locations(mod)
    ns = {"hmac": hmac, "log": _l.getLogger("t"), "CallerIdentity": object, "PermissionError": PermissionError}
    exec(compile(mod, "server.py:assert_owns", "exec"), ns)
    assert_owns = ns["_assert_owns"]
    me = _t.SimpleNamespace(tenant_id="a" * 64)
    # match -> ok; empty owner -> ok (new session); mismatch -> raises
    assert_owns(me, {"tenant_id": "a" * 64}, "t")
    assert_owns(me, {}, "t")
    import pytest as _p
    with _p.raises(PermissionError):
        assert_owns(me, {"tenant_id": "b" * 64}, "t")


RUNTIME = (CAPSULE / "rootfs/opt/capsule/desktop_brain_runtime.py").read_text()


def test_brain_domain_rejections_map_to_clean_409_not_500():
    """Live audit found unknown-task / already-active-task / freeze rejections collapsing into an
    opaque HTTP 500 (bridge_failure) — a model can't tell 'rejected because X' from 'bridge crashed'.
    They must map to a clean 409 with a machine code; a genuine RuntimeError bug still 500s."""
    # the bridge names the knowable brain rejections and 409s them
    assert "_BRAIN_ERROR_CODES" in BRIDGE
    for code in ("unknown_task", "task_already_active", "frozen"):
        assert code in BRIDGE
    assert "self._send(409" in BRIDGE
    assert "_is_brain_client_error" in BRIDGE  # gate so a real bug still falls through to 500
    # runtime.status returns a clean not-found instead of raising KeyError -> 500
    assert 'except KeyError:' in RUNTIME
    assert '"found": False' in RUNTIME


def test_double_click_always_opens_something_images_pdf_html_and_code():
    # Live-QA 2026-07-13: double-clicking an uploaded PNG dead-ended in "There is no app
    # installed for 'PNG image' files" — nothing in the image declared image/PDF MIME types.
    # Chromium IS the viewer (no extra packages): a desktop entry + system mimeapps defaults
    # route images/PDF/HTML to it, and text/code to GNOME Text Editor (every code type
    # subclasses text/plain in shared-mime-info, so the text/plain default is the catch-all).
    desktop = (CAPSULE / "rootfs/usr/share/applications/pairputer-browser.desktop").read_text()
    assert "Exec=/usr/local/bin/pairputer-chromium %U" in desktop
    for mime in ("image/png", "image/jpeg", "image/svg+xml", "application/pdf", "text/html"):
        assert mime in desktop
    mimeapps = (CAPSULE / "rootfs/usr/share/applications/mimeapps.list").read_text()
    assert "[Default Applications]" in mimeapps
    for line in ("image/png=pairputer-browser.desktop",
                 "application/pdf=pairputer-browser.desktop",
                 "text/html=pairputer-browser.desktop",
                 "text/plain=org.gnome.TextEditor.desktop",
                 "text/x-python=org.gnome.TextEditor.desktop",
                 "text/x-go=org.gnome.TextEditor.desktop",
                 "text/rust=org.gnome.TextEditor.desktop"):
        assert line in mimeapps
    # The wrapper must FORWARD file/URL args (nautilus %U) — it silently dropped them before,
    # which also broke the dock's "VS Code" button URL on first launch.
    assert '"${@:-about:blank}"' in CHROMIUM
    # The build fails loudly if the desktop database can't resolve the viewer.
    assert "update-desktop-database /usr/share/applications" in DOCKERFILE
    assert "desktop-file-utils" in DOCKERFILE
    assert 'grep -q "pairputer-browser.desktop" /usr/share/applications/mimeinfo.cache' in DOCKERFILE
