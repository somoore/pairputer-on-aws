#!/usr/bin/env python3.11
"""Single-client low-latency Opus stream on :6902."""

import asyncio
import contextlib
import logging
import os
import subprocess
import time

import websockets

logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
HOST, PORT = os.environ.get("PAIRPUTER_WS_BIND", "0.0.0.0"), 6902
FIRST_PAYLOAD_TIMEOUT_SECONDS = 10.0
DIAGNOSTIC_BYTE_LIMIT = 512
SLOT = asyncio.BoundedSemaphore(1)


def websocket_origin(ws):
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None) or getattr(ws, "request_headers", {})
    return headers.get("Origin")


def exact(stream, n):
    out = bytearray()
    while len(out) < n:
        data = stream.read(n - len(out))
        if not data:
            return b""
        out += data
    return bytes(out)


def page(stream):
    cap = exact(stream, 4)
    while cap and cap != b"OggS":
        cap = cap[1:] + exact(stream, 1)
    if not cap:
        return None
    header = exact(stream, 23)
    if not header:
        return None
    table = exact(stream, header[-1])
    data = exact(stream, sum(table))
    out = []
    start = acc = 0
    for lace in table:
        acc += lace
        if lace < 255:
            out.append(data[start : start + acc])
            start += acc
            acc = 0
    return out


def processes():
    parec = subprocess.Popen(
        [
            "parec",
            "--format=s16le",
            "--rate=48000",
            "--channels=2",
            "--latency-msec=30",
            "-d",
            "capsule.monitor",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-i",
            "pipe:0",
            "-c:a",
            "libopus",
            "-b:a",
            "96k",
            "-application",
            "audio",
            "-frame_duration",
            "20",
            "-page_duration",
            "20000",
            "-flush_packets",
            "1",
            "-f",
            "ogg",
            "pipe:1",
        ],
        stdin=parec.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    parec.stdout.close()
    return parec, ffmpeg


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


async def _audio_pump(ws, ffmpeg, first_payload, stats):
    loop = asyncio.get_running_loop()
    sent_head = False
    while True:
        packets = await loop.run_in_executor(None, page, ffmpeg.stdout)
        if packets is None:
            return
        for packet in packets:
            if (
                not packet
                or packet.startswith(b"OpusTags")
                or (packet.startswith(b"OpusHead") and sent_head)
            ):
                continue
            if packet.startswith(b"OpusHead"):
                sent_head = True
            await ws.send(packet)
            stats["payloads"] += 1
            first_payload.set()


async def _supervise_stream(ws, pump, first_payload):
    closed = asyncio.create_task(ws.wait_closed(), name="audio-client-closed")
    first = asyncio.create_task(first_payload.wait(), name="audio-first-payload")
    reason = "encoder_stopped"
    try:
        done, _ = await asyncio.wait(
            {pump, closed, first},
            timeout=FIRST_PAYLOAD_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            return "first_payload_timeout"
        if closed in done:
            return "client_closed"
        if pump in done:
            return reason

        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        done, _ = await asyncio.wait({pump, closed}, return_when=asyncio.FIRST_COMPLETED)
        return "client_closed" if closed in done else reason
    finally:
        for task in (closed, first):
            if not task.done():
                task.cancel()
        await asyncio.gather(closed, first, return_exceptions=True)


async def handler(ws):
    if websocket_origin(ws) is not None:
        await ws.close(code=1008, reason="direct browser audio is forbidden")
        return
    if SLOT.locked():
        await ws.close(code=1013, reason="audio stream already active")
        return

    await SLOT.acquire()
    parec = ffmpeg = None
    pump = None
    diagnostics = []
    first_payload = asyncio.Event()
    stats = {"payloads": 0}
    started = time.monotonic()
    reason = "setup_failed"
    try:
        parec, ffmpeg = processes()
        diagnostics = [
            asyncio.create_task(
                _drain_diagnostics(parec.stderr), name="audio-source-diagnostics"
            ),
            asyncio.create_task(
                _drain_diagnostics(ffmpeg.stderr), name="audio-encoder-diagnostics"
            ),
        ]
        pump = asyncio.create_task(
            _audio_pump(ws, ffmpeg, first_payload, stats), name="audio-encoder-pump"
        )
        reason = await _supervise_stream(ws, pump, first_payload)
    except Exception as exc:
        reason = f"handler_error:{type(exc).__name__}"
    finally:
        # Close both ends of the pipeline before cancelling the blocked pipe reader.
        await asyncio.gather(
            asyncio.to_thread(_stop_process, ffmpeg),
            asyncio.to_thread(_stop_process, parec),
        )
        if pump is not None and not pump.done():
            pump.cancel()
        if pump is not None:
            await asyncio.gather(pump, return_exceptions=True)
        diagnostic_bytes = 0
        if diagnostics:
            results = await asyncio.gather(*diagnostics, return_exceptions=True)
            diagnostic_bytes = sum(item for item in results if isinstance(item, int))
        diagnostic_bytes = min(diagnostic_bytes, DIAGNOSTIC_BYTE_LIMIT + 1)
        logging.warning(
            "audio stream ended reason=%s first_payload=%s payloads=%d source_rc=%s "
            "encoder_rc=%s diagnostic_bytes=%d diagnostic_truncated=%s elapsed_ms=%d",
            reason,
            first_payload.is_set(),
            stats["payloads"],
            parec.poll() if parec is not None else None,
            ffmpeg.poll() if ffmpeg is not None else None,
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
