import asyncio
import importlib.util
from pathlib import Path
import subprocess
import sys
import types
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def load_video_ws_module():
    path = REPO_ROOT / "capsules/hellbox-doom/rootfs/opt/capsule/video_ws.py"
    sys.modules.setdefault("websockets", types.SimpleNamespace(serve=None))
    spec = importlib.util.spec_from_file_location("video_ws_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DummyWebSocket:
    def __init__(self):
        self.close_calls = []

    async def close(self, code=None, reason=None):
        self.close_calls.append((code, reason))


class VideoStreamLimitTests(unittest.TestCase):
    def test_viewer_handoff_races_are_serialized_and_release_failure_is_fail_closed(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        lease_core = relay[relay.index("function closeViewerStreams(sess)"):
                           relay.index("function waitForViewerStreamRelease(signal)")]
        release_core = relay[relay.index("async function releaseHeld(sess, heldKeys, heldButtons)"):
                             relay.index("function readBody(req, maxBytes)")]
        harness = r'''
const assert = require("node:assert/strict");
const LEGACY_VIEWER = Symbol("legacy-viewer");
const MAX_RETIRED_VIEWERS = 128;
const VIEWER_STREAM_HANDOFF_MS = 2;
const VIEWER_STREAM_RESERVATION_TTL_MS = 20;
''' + lease_core + "\n" + release_core + r'''
let sent = [];
let releaseDelay = 0;
let releaseFails = false;
async function sendInput(_sess, event) {
  if (releaseDelay) await new Promise(resolve => setTimeout(resolve, releaseDelay));
  if (releaseFails) throw Object.assign(new Error("delivery failed"), { statusCode: 502 });
  sent.push(event);
  return true;
}
function makeSession(active = "old", generation = 1) {
  return {
    activeViewerId: active,
    activeViewerGeneration: generation,
    viewerAware: !!active,
    retiredViewerIds: new Set(),
    viewerReadyAt: 0,
    viewerClaimInflight: null,
    inputOperationInflight: null,
    viewerInputState: new Map(),
    eventSubscribers: new Set(),
    audioStreamCloser: null,
    videoStreamCloser: null,
    streamClosers: new Set(),
  };
}
const identity = value => ({ present: true, valid: true, value });
(async () => {
  const sess = makeSession();
  sess.viewerInputState.set("old", { heldKeys: new Set(["Shift"]), heldButtons: new Set([1]) });
  releaseDelay = 2;
  const [first, second] = await Promise.all([
    claimViewer(sess, identity("viewer_A")),
    claimViewer(sess, identity("viewer_B")),
  ]);
  assert.equal(first.ok, true);
  assert.equal(second.ok, true);
  assert.equal(sess.activeViewerId, "viewer_B");
  assert.equal(sess.activeViewerGeneration, 3);
  assert.equal(ownsViewerLease(sess, first.lease), false);
  assert.equal(ownsViewerLease(sess, second.lease), true);
  assert.deepEqual([...sess.retiredViewerIds], ["old", "viewer_A"]);
  assert.deepEqual(sent, [
    { t: "k", down: false, key: "Shift" },
    { t: "b", down: false, button: 1 },
  ]);
  assert.equal((await claimViewer(sess, identity("old"))).statusCode, 409);
  assert.equal((await claimViewer(sess, { present: false, valid: false, value: null })).statusCode, 409);
  assert.equal((await claimViewer(sess, { present: true, valid: false, value: null })).statusCode, 400);

  const held = { heldKeys: new Set(["Control"]), heldButtons: new Set() };
  sess.viewerInputState.set("viewer_B", held);
  releaseDelay = 0;
  releaseFails = true;
  const failed = await claimViewer(sess, identity("viewer_C"));
  assert.equal(failed.statusCode, 503);
  assert.equal(sess.activeViewerId, "viewer_B");
  assert.equal(sess.retiredViewerIds.has("viewer_B"), false);
  assert.equal(held.heldKeys.has("Control"), true);

  releaseFails = false;
  releaseDelay = 10;
  const abort = new AbortController();
  const abortedHandoff = claimViewer(sess, identity("viewer_D"), { signal: abort.signal });
  setTimeout(() => abort.abort(), 1);
  const aborted = await abortedHandoff;
  assert.equal(aborted.statusCode, 503);
  assert.equal(sess.activeViewerId, "viewer_B");
  assert.equal(sess.retiredViewerIds.has("viewer_B"), false);

  const wedged = makeSession("viewer_B", 7);
  wedged.videoStreamCloser = {
    lease: {viewerId: "viewer_B", generation: 7}, close: null, expiresAt: Date.now() - 1,
  };
  const recovered = await claimViewer(wedged, identity("viewer_C"));
  assert.equal(recovered.ok, true);
  assert.equal(wedged.activeViewerId, "viewer_C");
  assert.equal(wedged.videoStreamCloser, null);

  const capped = makeSession("active", 9);
  for (let i = 0; i < MAX_RETIRED_VIEWERS; i += 1) capped.retiredViewerIds.add("old_" + i);
  const limited = await claimViewer(capped, identity("new_viewer"));
  assert.equal(limited.statusCode, 429);
  assert.equal(capped.activeViewerId, "active");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_relay_player_handoff_retires_orphaned_viewer_streams(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        app = read_text("substrate/mcp-server/app.html")

        self.assertIn("activeViewerId: null", relay)
        self.assertIn("activeViewerGeneration: 0", relay)
        self.assertIn("retiredViewerIds: new Set()", relay)
        self.assertIn("async function claimViewer(sess, viewerIdentity, options = {})", relay)
        self.assertIn("function closeViewerStreams(sess)", relay)
        self.assertIn("function ownsViewerLease(sess, lease)", relay)
        self.assertIn("sess.activeViewerGeneration += 1", relay)
        self.assertIn("waitMs: oldLease ? VIEWER_STREAM_HANDOFF_MS : 0", relay)
        self.assertIn("MAX_RETIRED_VIEWERS", relay)
        self.assertNotIn("retiredViewerIds.delete", relay)
        self.assertIn("const VIEWER_ID=params.get('viewer')||", relay)
        self.assertIn("q.set('viewer',VIEWER_ID)", relay)
        self.assertIn("const RELAY_VIEWER_ID = crypto.randomUUID", app)
        self.assertIn("qs.set('viewer', RELAY_VIEWER_ID)", app)
        self.assertIn("q.set('viewer', RELAY_VIEWER_ID)", app)
        self.assertIn("'&viewer=' + encodeURIComponent(RELAY_VIEWER_ID)", app)

        claim_section = relay[relay.index("async function claimViewer(sess, viewerIdentity, options = {})"):]
        release_index = claim_section.index("await releaseHeld(sess, oldState.heldKeys, oldState.heldButtons)")
        retire_index = claim_section.index("sess.retiredViewerIds.add(oldLease.viewerId)")
        activate_index = claim_section.index("sess.activeViewerId = viewerId", retire_index)
        self.assertLess(release_index, retire_index)
        self.assertLess(retire_index, activate_index)

        handler_section = relay[relay.index("async function handleHttp(req, res)"):]
        claim_index = handler_section.index("viewerLease = await claimViewer(sess, requestViewer, { signal: abortController.signal })")
        reserve_index = handler_section.index("if (isVideo) {\n      if (!await reserveVideoStream(sess, viewerLease.lease, { signal: abortController.signal }))")
        self.assertLess(claim_index, reserve_index)

    def test_viewer_id_absence_is_distinct_from_invalid_and_legacy_cannot_bypass_active_lease(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        parser = relay[relay.index("function viewerIdFrom(reqUrl)"):relay.index("function closeViewerStreams")]
        claim = relay[relay.index("async function claimViewer(sess, viewerIdentity, options = {})"):relay.index("function reserveAudioStream")]

        self.assertIn('return { present: false, valid: false, value: null }', parser)
        self.assertIn('return { present: true, valid: false, value: null }', parser)
        self.assertIn('return { present: true, valid: true, value }', parser)
        self.assertIn('if (sess.viewerAware) return { ok: false, statusCode: 409, reason: "viewer id required" }', claim)
        self.assertIn('return { ok: false, statusCode: 400, reason: "invalid viewer id" }', claim)
        self.assertIn("sess.activeViewerId === lease.viewerId", relay)
        self.assertIn("sess.activeViewerGeneration === lease.generation", relay)

    def test_relay_reserves_one_video_stream_before_upstream_open(self):
        relay = read_text("substrate/stateful-relay/index.mjs")

        self.assertIn("videoStreamCloser: null", relay)
        self.assertIn("async function reserveVideoStream(sess, lease, options = {})", relay)
        self.assertIn("function attachVideoStream(sess, lease, closeUpstream)", relay)
        self.assertIn("function releaseVideoStream(sess, lease, closeUpstream = null)", relay)
        self.assertIn('text(res, 409, "video stream already active")', relay)
        self.assertIn("sess.videoStreamCloser = null;", relay)

        handler_section = relay[relay.index("async function handleHttp(req, res)"):]
        claim_index = handler_section.index("viewerLease = await claimViewer(sess, requestViewer, { signal: abortController.signal })")
        reserve_index = handler_section.index("if (isVideo) {\n      if (!await reserveVideoStream(sess, viewerLease.lease, { signal: abortController.signal }))")
        require_running_index = handler_section.index("await requireRunning(claims);")
        open_video_index = handler_section.index('isAudio ? "/pairputer/audio" : "/pairputer/video"')
        after_open_revalidation = handler_section.index("upstreamClosed || up.isClosed()", open_video_index)
        early_abort = handler_section.index('req.once("aborted", markStreamAborted)')

        self.assertLess(early_abort, reserve_index)
        self.assertLess(claim_index, reserve_index)
        self.assertLess(reserve_index, require_running_index)
        self.assertLess(require_running_index, open_video_index)
        self.assertGreater(after_open_revalidation, open_video_index)

    def test_same_viewer_channel_reconnect_atomically_replaces_attached_stream(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        reserve_core = relay[relay.index("function waitForViewerStreamRelease(signal)"):
                             relay.index("function attachAudioStream")]
        harness = r'''
const assert = require("node:assert/strict");
const VIEWER_STREAM_HANDOFF_MS = 2;
const VIEWER_STREAM_RESERVATION_TTL_MS = 20;
function sameViewerLease(a,b){return !!a&&!!b&&a.viewerId===b.viewerId&&a.generation===b.generation;}
function ownsViewerLease(sess,lease){return sess.activeViewerId===lease.viewerId&&sess.activeViewerGeneration===lease.generation;}
async function withSessionLock(sess,field,fn){const previous=sess[field];let release;const current=new Promise(r=>release=r);sess[field]=current;if(previous)await previous;try{return await fn();}finally{release();if(sess[field]===current)sess[field]=null;}}
''' + reserve_core + r'''
(async()=>{
  const lease={viewerId:"same_viewer",generation:4};
  let closed=0;
  const oldClose=()=>{closed+=1;};
  const sess={activeViewerId:lease.viewerId,activeViewerGeneration:lease.generation,
    audioStreamCloser:{lease,close:oldClose},audioReservationInflight:null,streamClosers:new Set([oldClose])};
  const first=reserveAudioStream(sess,lease);
  await new Promise(r=>setTimeout(r,0));
  const third=reserveAudioStream(sess,lease);
  assert.equal(await first,true);
  assert.equal(await third,false);
  assert.equal(closed,1);
  assert.equal(sess.audioStreamCloser.close,null);
  assert.equal(sess.streamClosers.has(oldClose),false);

  await new Promise(r=>setTimeout(r,VIEWER_STREAM_RESERVATION_TTL_MS+2));
  assert.equal(await reserveAudioStream(sess,lease),true);
  assert.equal(sess.audioStreamCloser.close,null);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_player_token_refresh_is_noop_when_credentials_are_unchanged(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        player = relay[relay.index("function playerHtml()"):
                       relay.index("async function authorize(reqUrl, channel)")]
        self.assertIn("if(nextToken===TOK&&nextEdge===EDGE_AUTH)return", player)
        self.assertIn("const wasStarted=started;TOK=nextToken;EDGE_AUTH=nextEdge", player)
        self.assertIn("setTimeout(()=>{streamRestartTimer=null;startStreams();},200)", player)
        self.assertNotIn("if(started){stopStreams();startStreams();}", player)

    def test_standalone_player_reports_expired_session_instead_of_endless_reconnect(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        player = relay[relay.index("function playerHtml()"):
                       relay.index("async function authorize(reqUrl, channel)")]
        self.assertIn("function relayTokenExpired()", player)
        self.assertIn("parent===window&&relayTokenExpired()", player)
        self.assertIn("session expired · reconnect to continue", player)

    def test_input_delivery_is_awaited_and_held_state_is_viewer_scoped(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        app = read_text("substrate/mcp-server/app.html")
        input_handler = relay[relay.index("async function handleInputPost"):relay.index("async function drainSession")]
        release = relay[relay.index("async function releaseHeld"):relay.index("function readBody")]
        drain = relay[relay.index("async function drainSession"):relay.index("async function drainTenantMicrovmSessions")]

        self.assertIn("viewerInputState: new Map()", relay)
        self.assertIn("const state = getViewerInputState(sess, viewerLease.lease)", input_handler)
        self.assertIn("await sendInput(sess, event)", input_handler)
        self.assertNotIn("sendInput(sess, event).catch", input_handler)
        self.assertIn("for (const event of events) await sendInput(sess, event)", release)
        self.assertIn("heldKeys.clear()", release)
        self.assertIn("await releaseHeld(sess, state.heldKeys, state.heldButtons)", drain)
        self.assertIn("if (releaseFailed)", drain)
        self.assertIn("sess.draining = false", drain)
        # Freeze contract (2026-07-12): the authoritative MCP freeze runs regardless of the relay
        # drain (the drain is best-effort, wrapped in try/catch), and a drain/freeze error must NOT
        # roll the lifecycle intent back to running — that was the ~4s auto-thaw. The old
        # `if (!relayDrained)` rollback is gone; intent is committed to 'frozen' before any drain.
        self.assertIn("try { await playerRpcWithRefresh('freeze', {}, { timeoutMs: 8000 }); } catch {}", app)
        self.assertNotIn("if (!relayDrained)", app)
        self.assertIn("setIntent('frozen')", app)
        self.assertIn("function canAutoWake()", app)

    def test_events_use_one_bounded_fresh_viewer_bound_fanout(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        events = relay[relay.index("async function pollSessionEvents"):relay.index("function playerHtml")]

        self.assertIn("MAX_EVENT_SUBSCRIBERS = 4", relay)
        self.assertIn("MAX_EVENT_PAYLOAD_BYTES", events)
        self.assertIn("MAX_EVENT_BUFFER_BYTES", relay)
        self.assertIn("sess.eventSubscribers", events)
        self.assertIn("ownsViewerLease(sess, subscriber.lease)", events)
        self.assertIn("current = await loadActiveSessionCoalesced(sess.claims)", events)
        self.assertIn("sessionClaimsFresh(sess.claims, current)", events)
        self.assertIn("subscriber.res.writableLength > MAX_EVENT_BUFFER_BYTES", relay)
        self.assertIn("if (!subscriber.res.write(frame))", relay)
        self.assertEqual(events.count('upstreamHttpGet(sess, "/", COPLAY_PORT, {'), 1)
        self.assertIn("totalTimeoutMs: STREAM_REVALIDATE_MS - EVENT_POLL_INTERVAL_MS", events)

    def test_upstream_diagnostics_do_not_echo_status_lines_or_exception_messages(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        upstream = relay[relay.index("async function openUpstream"):relay.index("async function ensureInputUpstream")]
        http_handler = relay[relay.index("async function handleHttp(req, res)"):]

        self.assertNotIn('head.split("\\r\\n"', upstream)
        self.assertNotIn("err.message", upstream)
        self.assertIn('close("handshake_rejected")', upstream)
        self.assertIn('close("upstream_socket_error")', upstream)
        self.assertNotIn("text(res, statusCode, err.message", http_handler)
        self.assertNotIn('JSON.stringify({ error: String(err.message', http_handler)

    def test_guest_video_slot_rejects_second_stream(self):
        video_ws = load_video_ws_module()

        async def scenario():
            first = DummyWebSocket()
            second = DummyWebSocket()
            third = DummyWebSocket()

            self.assertTrue(await video_ws._acquire_video_slot(first))
            self.assertFalse(await video_ws._acquire_video_slot(second))
            self.assertEqual(second.close_calls, [(1013, "video stream already active")])

            video_ws._video_stream_slots.release()
            self.assertTrue(await video_ws._acquire_video_slot(third))
            video_ws._video_stream_slots.release()

        asyncio.run(scenario())

    def test_guest_handler_acquires_slot_before_starting_ffmpeg(self):
        video = read_text("capsules/hellbox-doom/rootfs/opt/capsule/video_ws.py")

        acquire_index = video.index("if not await _acquire_video_slot(ws):")
        pipeline_index = video.index("proc = _start_ffmpeg()")
        release_index = video.index("_video_stream_slots.release()")

        self.assertLess(acquire_index, pipeline_index)
        self.assertGreater(release_index, pipeline_index)


if __name__ == "__main__":
    unittest.main()
