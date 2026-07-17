import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def load_audio_ws_module():
    path = REPO_ROOT / "capsules/hellbox-doom/rootfs/opt/capsule/audio_ws.py"
    sys.modules.setdefault("websockets", types.SimpleNamespace(serve=None))
    spec = importlib.util.spec_from_file_location("audio_ws_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DummyWebSocket:
    def __init__(self):
        self.close_calls = []

    async def close(self, code=None, reason=None):
        self.close_calls.append((code, reason))


class AudioStreamLimitTests(unittest.TestCase):
    def test_relay_reserves_one_audio_stream_before_upstream_open(self):
        relay = read_text("substrate/stateful-relay/index.mjs")

        self.assertIn("audioStreamCloser: null", relay)
        self.assertIn("async function reserveAudioStream(sess, lease, options = {})", relay)
        self.assertIn("function attachAudioStream(sess, lease, closeUpstream)", relay)
        self.assertIn("function releaseAudioStream(sess, lease, closeUpstream = null)", relay)
        self.assertIn('text(res, 409, "audio stream already active")', relay)
        self.assertIn("sess.audioStreamCloser = null;", relay)

        handler_section = relay[relay.index("async function handleHttp(req, res)"):]
        claim_index = handler_section.index(
            "viewerLease = await claimViewer(sess, requestViewer, { signal: abortController.signal })")
        reserve_index = handler_section.index(
            "if (isAudio) {\n      if (!await reserveAudioStream(sess, viewerLease.lease, { signal: abortController.signal }))")
        require_running_index = handler_section.index("await requireRunning(claims);")
        open_audio_index = handler_section.index('isAudio ? "/pairputer/audio" : "/pairputer/video"')

        self.assertLess(claim_index, reserve_index)
        self.assertLess(reserve_index, require_running_index)
        self.assertLess(require_running_index, open_audio_index)

    def test_guest_audio_slot_rejects_second_stream(self):
        audio_ws = load_audio_ws_module()

        async def scenario():
            first = DummyWebSocket()
            second = DummyWebSocket()
            third = DummyWebSocket()

            self.assertTrue(await audio_ws._acquire_audio_slot(first))
            self.assertFalse(await audio_ws._acquire_audio_slot(second))
            self.assertEqual(second.close_calls, [(1013, "audio stream already active")])

            audio_ws._audio_stream_slots.release()
            self.assertTrue(await audio_ws._acquire_audio_slot(third))
            audio_ws._audio_stream_slots.release()

        asyncio.run(scenario())

    def test_guest_handler_acquires_slot_before_starting_pipeline(self):
        audio = read_text("capsules/hellbox-doom/rootfs/opt/capsule/audio_ws.py")

        acquire_index = audio.index("if not await _acquire_audio_slot(ws):")
        pipeline_index = audio.index("parec, ffmpeg = _start_pipeline()")
        release_index = audio.index("_audio_stream_slots.release()")

        self.assertLess(acquire_index, pipeline_index)
        self.assertGreater(release_index, pipeline_index)


if __name__ == "__main__":
    unittest.main()
