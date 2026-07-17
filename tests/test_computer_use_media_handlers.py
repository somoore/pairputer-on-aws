import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, mock


ROOT = Path(__file__).resolve().parents[1]
MEDIA_ROOT = (
    ROOT / "capsules" / "computer-use-desktop" / "rootfs" / "opt" / "capsule"
)


def load_module(name):
    path = MEDIA_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"workbench_{name}_under_test", path)
    module = importlib.util.module_from_spec(spec)
    fake_websockets = types.SimpleNamespace(serve=None)
    with mock.patch.dict(sys.modules, {"websockets": fake_websockets}):
        spec.loader.exec_module(module)
    return module


class FakeWebSocket:
    def __init__(self, *, origin=None, initially_closed=False):
        self.request_headers = {} if origin is None else {"Origin": origin}
        self.closed = asyncio.Event()
        if initially_closed:
            self.closed.set()
        self.close_calls = []

    async def wait_closed(self):
        await self.closed.wait()

    async def close(self, **kwargs):
        self.close_calls.append(kwargs)
        self.closed.set()


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stderr = None
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed += 1
        self.returncode = -9


async def blocked_pump(*_args):
    await asyncio.Event().wait()


class VideoHandlerLifecycleTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.video = load_module("video_ws")
        self.video.SLOT = asyncio.BoundedSemaphore(1)

    async def test_disconnect_terminates_encoder_cancels_pump_and_releases_slot(self):
        ws = FakeWebSocket(initially_closed=True)
        proc = FakeProcess()
        pump_finished = asyncio.Event()

        async def pump(*_args):
            try:
                await asyncio.Event().wait()
            finally:
                pump_finished.set()

        with mock.patch.object(self.video, "process", return_value=proc), mock.patch.object(
            self.video, "_video_pump", side_effect=pump
        ):
            await self.video.handler(ws)

        self.assertEqual(proc.terminated, 1)
        self.assertTrue(pump_finished.is_set())
        self.assertFalse(self.video.SLOT.locked())

    async def test_missing_first_payload_times_out_and_releases_slot(self):
        ws = FakeWebSocket()
        proc = FakeProcess()

        with mock.patch.object(self.video, "process", return_value=proc), mock.patch.object(
            self.video, "_video_pump", side_effect=blocked_pump
        ), mock.patch.object(self.video, "FIRST_PAYLOAD_TIMEOUT_SECONDS", 0.01):
            await asyncio.wait_for(self.video.handler(ws), timeout=1)

        self.assertEqual(proc.terminated, 1)
        self.assertFalse(self.video.SLOT.locked())

    async def test_origin_guard_and_busy_slot_still_reject(self):
        origin_ws = FakeWebSocket(origin="https://untrusted.example")
        with mock.patch.object(self.video, "process") as spawn:
            await self.video.handler(origin_ws)
        spawn.assert_not_called()
        self.assertEqual(origin_ws.close_calls[0]["code"], 1008)

        await self.video.SLOT.acquire()
        try:
            busy_ws = FakeWebSocket()
            await self.video.handler(busy_ws)
        finally:
            self.video.SLOT.release()
        self.assertEqual(busy_ws.close_calls[0]["code"], 1013)


class AudioHandlerLifecycleTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.audio = load_module("audio_ws")
        self.audio.SLOT = asyncio.BoundedSemaphore(1)

    async def test_disconnect_terminates_pipeline_cancels_pump_and_releases_slot(self):
        ws = FakeWebSocket(initially_closed=True)
        parec, ffmpeg = FakeProcess(), FakeProcess()
        pump_finished = asyncio.Event()

        async def pump(*_args):
            try:
                await asyncio.Event().wait()
            finally:
                pump_finished.set()

        with mock.patch.object(
            self.audio, "processes", return_value=(parec, ffmpeg)
        ), mock.patch.object(self.audio, "_audio_pump", side_effect=pump):
            await self.audio.handler(ws)

        self.assertEqual(parec.terminated, 1)
        self.assertEqual(ffmpeg.terminated, 1)
        self.assertTrue(pump_finished.is_set())
        self.assertFalse(self.audio.SLOT.locked())

    async def test_missing_first_payload_times_out_and_releases_slot(self):
        ws = FakeWebSocket()
        parec, ffmpeg = FakeProcess(), FakeProcess()

        with mock.patch.object(
            self.audio, "processes", return_value=(parec, ffmpeg)
        ), mock.patch.object(
            self.audio, "_audio_pump", side_effect=blocked_pump
        ), mock.patch.object(self.audio, "FIRST_PAYLOAD_TIMEOUT_SECONDS", 0.01):
            await asyncio.wait_for(self.audio.handler(ws), timeout=1)

        self.assertEqual(parec.terminated, 1)
        self.assertEqual(ffmpeg.terminated, 1)
        self.assertFalse(self.audio.SLOT.locked())

    async def test_origin_guard_and_busy_slot_still_reject(self):
        origin_ws = FakeWebSocket(origin="https://untrusted.example")
        with mock.patch.object(self.audio, "processes") as spawn:
            await self.audio.handler(origin_ws)
        spawn.assert_not_called()
        self.assertEqual(origin_ws.close_calls[0]["code"], 1008)

        await self.audio.SLOT.acquire()
        try:
            busy_ws = FakeWebSocket()
            await self.audio.handler(busy_ws)
        finally:
            self.audio.SLOT.release()
        self.assertEqual(busy_ws.close_calls[0]["code"], 1013)
