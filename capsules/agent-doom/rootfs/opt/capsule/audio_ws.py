#!/usr/bin/env python3.11
"""Opus audio WebSocket on :6902.

Frame 1 is OpusHead. Later frames are raw 20 ms Opus packets.
"""
import asyncio
import os
import subprocess
import time
import websockets

DEVICE = "capsule.monitor"
PORT = 6902
HOST = os.environ.get("PAIRPUTER_WS_BIND", "0.0.0.0")
RATE = 48000
CHANNELS = 2
BITRATE = "96k"
_audio_stream_slots = asyncio.BoundedSemaphore(1)


async def _acquire_audio_slot(ws):
    """Reserve the only audio capture/encode slot for this MicroVM."""
    if _audio_stream_slots.locked():
        try:
            await ws.close(code=1013, reason="audio stream already active")
        except Exception:
            pass
        return False
    await _audio_stream_slots.acquire()
    return True


def _dbg_log():
    # /tmp, not /var/log: audio_ws runs as 'app' (uid 1000) and /var/log is
    # 0755 root:root — a /var/log open() silently fell to DEVNULL, so /dbg/audio
    # read EMPTY for the whole investigation. /tmp is app-writable (like
    # /tmp/audiows.log). ready-gate.sh reads it as root over vsock :9000.
    try:
        return open("/tmp/audio_dbg.log", "ab", buffering=0)
    except Exception:
        return subprocess.DEVNULL


def _wait_for_source(dbg, tries=15):
    """Wait for the pulse capsule.monitor source before spawning parec.

    The real audio bug: parec spawned once against an absent capsule.monitor
    dies instantly, ffmpeg emits only OpusHead then EOFs (":6902 = 19-byte
    header then closes"). run_app.sh's pulse watchdog can drop+recreate the
    null-sink, so a connect can race it. Poll instead of spawning blind.
    """
    for _ in range(tries):
        try:
            out = subprocess.run(["pactl", "list", "short", "sources"],
                                 capture_output=True, timeout=3).stdout
            if b"capsule.monitor" in out:
                return True
        except Exception:
            pass
        _write(dbg, "waiting for capsule.monitor source...\n")
        time.sleep(1)
    _write(dbg, "capsule.monitor NEVER appeared -- null-sink dead/absent\n")
    return False


def _write(dbg, msg):
    try:
        dbg.write(msg.encode())
    except Exception:
        pass


def _start_pipeline():
    """Start parec -> ffmpeg(libopus/ogg)."""
    # DIAG: tee parec + ffmpeg stderr to /var/log/audio_dbg.log (read over vsock
    # :9000/dbg/audio) so a silent capsule shows the cause — parec failing to open
    # the pulse capsule.monitor source (dead sink / SDL-vs-pulse race) vs an ffmpeg
    # opus error. ":6902 serves Opus" != audible; this names which side is broken.
    _dbg = _dbg_log()
    _write(_dbg, "--- pipeline start ---\n")
    _wait_for_source(_dbg)
    parec = subprocess.Popen(
        ["parec", "--format=s16le", f"--rate={RATE}", f"--channels={CHANNELS}",
         "--latency-msec=30", "-d", DEVICE],
        stdout=subprocess.PIPE, stderr=_dbg,
    )
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "warning",
         "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
         "-c:a", "libopus", "-b:a", BITRATE, "-application", "audio",
         "-frame_duration", "20",
         # One Ogg page per packet keeps latency near one frame.
         "-page_duration", "20000", "-flush_packets", "1",
         "-f", "ogg", "pipe:1"],
        stdin=parec.stdout, stdout=subprocess.PIPE, stderr=_dbg,
    )
    if parec.stdout:
        parec.stdout.close()
    return parec, ffmpeg


def _read_exactly(stream, n):
    """Read exactly n bytes, or b'' on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return b""
        buf += chunk
    return bytes(buf)


def _read_ogg_page(stream):
    """Read one Ogg page and return complete Opus packets."""
    # Resync to OggS after any partial read.
    cap = _read_exactly(stream, 4)
    if not cap:
        return None
    while cap != b"OggS":
        nxt = stream.read(1)
        if not nxt:
            return None
        cap = cap[1:] + nxt
    header = _read_exactly(stream, 23)  # everything up to and incl. nsegs
    if not header:
        return None
    nsegs = header[-1]
    seg_table = _read_exactly(stream, nsegs) if nsegs else b""
    data_len = sum(seg_table)
    data = _read_exactly(stream, data_len) if data_len else b""
    if data_len and not data:
        return None

    packets, pos, cur = [], 0, 0
    for lace in seg_table:
        cur += lace
        if lace < 255:  # packet boundary
            packets.append(data[pos:pos + cur])
            pos += cur
            cur = 0
    return packets


async def handler(ws):
    if not await _acquire_audio_slot(ws):
        return
    parec, ffmpeg = None, None
    try:
        parec, ffmpeg = _start_pipeline()
        loop = asyncio.get_event_loop()
        sent_head = False
        while True:
            packets = await loop.run_in_executor(None, _read_ogg_page, ffmpeg.stdout)
            if packets is None:
                break
            for pkt in packets:
                if not pkt:
                    continue
                if pkt.startswith(b"OpusTags"):
                    continue  # comment header — client doesn't need it
                if pkt.startswith(b"OpusHead"):
                    if sent_head:
                        continue
                    sent_head = True
                await ws.send(pkt)
    except Exception:
        pass
    finally:
        for p in (ffmpeg, parec):
            if not p:
                continue
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        _audio_stream_slots.release()


async def main():
    async with websockets.serve(handler, HOST, PORT, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
