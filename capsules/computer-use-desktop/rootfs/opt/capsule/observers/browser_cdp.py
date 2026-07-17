"""Minimal CDP client; the endpoint is always validated as loopback."""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.parse
import urllib.request


class _CdpSession:
    """One bounded CDP WebSocket session.

    Frontend DOM ``nodeId`` values are session-scoped, so callers that traverse the DOM must keep the
    same connection from ``DOM.getDocument`` through subsequent DOM/Accessibility commands.
    """

    def __init__(self, websocket_url: str, timeout: float):
        parsed = urllib.parse.urlparse(websocket_url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("CDP websocket must be loopback")
        self.websocket_url = websocket_url
        self.timeout = timeout
        self.sequence = 0
        self.ws = None

    def __enter__(self):
        from websocket import create_connection
        self.ws = create_connection(
            self.websocket_url, timeout=self.timeout, origin="http://127.0.0.1")
        return self

    def command(self, method, params=None):
        if self.ws is None:
            raise RuntimeError("CDP session is not open")
        self.sequence += 1
        ident = self.sequence
        self.ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv()
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > 2 * 1024 * 1024:
                raise ValueError("CDP websocket response too large")
            value = json.loads(raw)
            if value.get("id") == ident:
                if "error" in value:
                    raise RuntimeError(str(value["error"]))
                return value.get("result", {})

    def __exit__(self, _exc_type, _exc, _traceback):
        if self.ws is not None:
            self.ws.close()
            self.ws = None


class CdpClient:
    def __init__(self, endpoint="http://127.0.0.1:9222", timeout=2.0):
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("CDP endpoint must be loopback HTTP")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 80)}
        except socket.gaierror as exc:
            raise ValueError("CDP endpoint does not resolve") from exc
        if not addresses or any(not ipaddress.ip_address(addr).is_loopback for addr in addresses):
            raise ValueError("CDP endpoint must resolve only to loopback")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def _json(self, path, method="GET", timeout=None):
        req = urllib.request.Request(self.endpoint + path, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as response:
            if int(response.headers.get("Content-Length", "0") or 0) > 2 * 1024 * 1024:
                raise ValueError("CDP response too large")
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("CDP response too large")
        return json.loads(data)

    def tabs(self):
        return [item for item in self._json("/json/list") if item.get("type") == "page"][:50]

    def new_tab(self, url):
        quoted = urllib.parse.quote(url, safe="")
        # Target creation can be materially slower than an ordinary loopback CDP read on a cold
        # MicroVM. The broker separately reconciles a timed-out, already-created target and never
        # blindly replays this mutation.
        return self._json(f"/json/new?{quoted}", method="PUT", timeout=max(self.timeout, 10.0))

    def session(self, websocket_url):
        return _CdpSession(websocket_url, self.timeout)

    def command(self, websocket_url, method, params=None):
        with self.session(websocket_url) as session:
            return session.command(method, params)
