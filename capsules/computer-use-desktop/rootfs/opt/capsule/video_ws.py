#!/usr/bin/env python3.11
"""Single-client low-latency H.264 desktop stream on :6903."""

import asyncio
import contextlib
import logging
import os
import subprocess
import time

import websockets

logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
HOST, PORT = os.environ.get("PAIRPUTER_WS_BIND", "0.0.0.0"), 6903
DISPLAY = os.environ.get("DISPLAY", ":1") + ".0"
FPS, GOP = 30, 60
FIRST_PAYLOAD_TIMEOUT_SECONDS = 10.0
DIAGNOSTIC_BYTE_LIMIT = 512
SLOT = asyncio.BoundedSemaphore(1)


def websocket_origin(ws):
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None) or getattr(ws, "request_headers", {})
    return headers.get("Origin")


def size():
    try:
        from Xlib import display

        d = display.Display(os.environ.get("DISPLAY", ":1"))
        s = d.screen()
        value = (s.width_in_pixels // 2 * 2, s.height_in_pixels // 2 * 2)
        d.close()
        return value
    except Exception:
        return (1440, 900)


def process():
    w, h = size()
    return subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "x11grab",
            "-framerate",
            str(FPS),
            "-video_size",
            f"{w}x{h}",
            "-i",
            DISPLAY,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "baseline",
            "-level",
            "4.0",
            "-pix_fmt",
            "yuv420p",
            "-x264-params",
            f"keyint={GOP}:min-keyint={GOP}:scenecut=0:aud=1:repeat-headers=1",
            "-f",
            "h264",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def boundary(buf, start=1):
    pos = start
    while True:
        i = buf.find(b"\x00\x00\x01\x09", pos)
        if i < 0:
            return None
        j = i - 1 if i and buf[i - 1] == 0 else i
        if j > 0:
            return j
        pos = i + 4


def keyframe(au):
    pos = 0
    while True:
        i = au.find(b"\x00\x00\x01", pos)
        if i < 0:
            return False
        if i + 3 < len(au) and au[i + 3] & 31 in (5, 7):
            return True
        pos = i + 3


async def _drain_diagnostics(stream):
    """Drain stderr without retaining or logging its potentially sensitive text."""
    if stream is None:
        return 0
    loop = asyncio.get_running_loop()
    observed = 0
    while True:
        chunk = await loop.run_in_executor(None, stream.read1, 4096)
        if not chunk:
            return observed
        observed = min(DIAGNOSTIC_BYTE_LIMIT + 1, observed + len(chunk))


def _stop_process(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except Exception:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)


async def _video_pump(ws, proc, first_payload, stats):
    buf = b""
    loop = asyncio.get_running_loop()
    while True:
        chunk = await loop.run_in_executor(None, proc.stdout.read1, 65536)
        if not chunk:
            return
        buf += chunk
        while True:
            nxt = boundary(buf)
            if nxt is None:
                break
            au, buf = buf[:nxt], buf[nxt:]
            if not au:
                continue
            await ws.send((b"\x01" if keyframe(au) else b"\x00") + au)
            stats["payloads"] += 1
            first_payload.set()


async def _supervise_stream(ws, proc, pump, first_payload):
    closed = asyncio.create_task(ws.wait_closed(), name="video-client-closed")
    first = asyncio.create_task(first_payload.wait(), name="video-first-payload")
    reason = "encoder_stopped"
    try:
        done, _ = await asyncio.wait(
            {pump, closed, first},
            timeout=FIRST_PAYLOAD_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            reason = "first_payload_timeout"
            return reason
        if closed in done:
            reason = "client_closed"
            return reason
        if pump in done:
            return reason

        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        done, _ = await asyncio.wait({pump, closed}, return_when=asyncio.FIRST_COMPLETED)
        reason = "client_closed" if closed in done else "encoder_stopped"
        return reason
    finally:
        for task in (closed, first):
            if not task.done():
                task.cancel()
        await asyncio.gather(closed, first, return_exceptions=True)


async def handler(ws):
    if websocket_origin(ws) is not None:
        await ws.close(code=1008, reason="direct browser video is forbidden")
        return
    if SLOT.locked():
        await ws.close(code=1013, reason="video stream already active")
        return

    await SLOT.acquire()
    proc = None
    pump = diagnostics = None
    first_payload = asyncio.Event()
    stats = {"payloads": 0}
    started = time.monotonic()
    reason = "setup_failed"
    try:
        proc = process()
        diagnostics = asyncio.create_task(
            _drain_diagnostics(proc.stderr), name="video-encoder-diagnostics"
        )
        pump = asyncio.create_task(
            _video_pump(ws, proc, first_payload, stats), name="video-encoder-pump"
        )
        reason = await _supervise_stream(ws, proc, pump, first_payload)
    except Exception as exc:
        reason = f"handler_error:{type(exc).__name__}"
    finally:
        # Stopping the producer first unblocks any executor thread waiting on a pipe.
        await asyncio.to_thread(_stop_process, proc)
        if pump is not None and not pump.done():
            pump.cancel()
        if pump is not None:
            await asyncio.gather(pump, return_exceptions=True)
        diagnostic_bytes = 0
        if diagnostics is not None:
            result = await asyncio.gather(diagnostics, return_exceptions=True)
            if result and isinstance(result[0], int):
                diagnostic_bytes = result[0]
        logging.warning(
            "video stream ended reason=%s first_payload=%s payloads=%d rc=%s "
            "diagnostic_bytes=%d diagnostic_truncated=%s elapsed_ms=%d",
            reason,
            first_payload.is_set(),
            stats["payloads"],
            proc.poll() if proc is not None else None,
            min(diagnostic_bytes, DIAGNOSTIC_BYTE_LIMIT),
            diagnostic_bytes > DIAGNOSTIC_BYTE_LIMIT,
            int((time.monotonic() - started) * 1000),
        )
        SLOT.release()


async def main():
    async with websockets.serve(handler, HOST, PORT, max_size=256 * 1024):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
