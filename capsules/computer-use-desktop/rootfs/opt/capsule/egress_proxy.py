#!/usr/bin/env python3.11
"""Bounded, fail-closed HTTP(S) egress proxy for the visible Chromium.

Chromium is forced through this loopback listener for HTTP, HTTPS, and WebSocket
traffic.  The proxy resolves each destination exactly once, validates every
returned address, and connects to the selected numeric address.  This makes the
network enforcement independent of page-controlled redirects and subresources
and closes the DNS-rebinding gap left by URL-time policy checks.

The service deliberately logs no request targets, URLs, or headers.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import selectors
import socket
import socketserver
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Callable, Iterable


LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("PAIRPUTER_EGRESS_PROXY_PORT", "6907"))
MAX_HEADER_BYTES = 32 * 1024
MAX_REQUEST_LINE = 8 * 1024
MAX_HEADER_COUNT = 100
MAX_STREAM_BYTES = 256 * 1024 * 1024
HEADER_TIMEOUT = 5.0
CONNECT_TIMEOUT = 5.0
DNS_TIMEOUT = 3.0
IDLE_TIMEOUT = 60.0
MAX_CONNECTION_LIFETIME = 10 * 60.0
# A loopback preview-grant WebSocket (code-server/VS Code, HMR) is long-lived and often quiet: it
# outlives a 10 min HTTP-fetch cap and idles longer than 60s between frames. These looser bounds
# apply ONLY to the in-box, broker-authorized loopback tunnel — never to real egress.
WS_IDLE_TIMEOUT = 5 * 60.0
WS_MAX_LIFETIME = 8 * 60 * 60.0
WS_MAX_STREAM_BYTES = 4 * 1024 * 1024 * 1024
MAX_CONCURRENT = 32
PROTECTED_PORTS = frozenset({5901, *range(6901, 6908), 9000, 9222, 50051})
PUBLIC_PORTS = frozenset({80, 443})
_METHOD = re.compile(r"^[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}$")
_HEADER_NAME = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_FORWARD_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_METADATA_NAMES = frozenset({
    "metadata.google.internal", "metadata.azure.internal", "metadata.aws.internal",
})
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="egress-dns")
_PREVIEW_HOST_RE = re.compile(r"^p-([a-f0-9]{43})\.pairputer-preview\.invalid$")


class ProxyRequestError(ValueError):
    status = 400


class ProxyPolicyDenied(ProxyRequestError):
    status = 403


class ProxyHeadersTooLarge(ProxyRequestError):
    status = 431


class ProxyUpstreamError(OSError):
    status = 502


@dataclass(frozen=True)
class ResolvedTarget:
    host: str
    port: int
    ip: str
    family: int
    socktype: int
    proto: int
    sockaddr: tuple


def _parse_ports(raw: str | Iterable[int]) -> frozenset[int]:
    values = raw.split(",") if isinstance(raw, str) else raw
    result: set[int] = set()
    for item in values or ():
        text = str(item).strip()
        if not text:
            continue
        try:
            if "-" in text:
                first, last = (int(value) for value in text.split("-", 1))
            else:
                first = last = int(text)
        except ValueError as exc:
            raise ValueError("invalid egress port policy") from exc
        if first < 1 or last > 65535 or first > last or last - first > 10000:
            raise ValueError("invalid egress port policy")
        result.update(range(first, last + 1))
    if not result:
        raise ValueError("egress port policy must not be empty")
    return frozenset(result)


class EgressPolicy:
    """Resolve once, validate all answers, and return one numeric endpoint."""

    def __init__(
        self,
        *,
        resolver: Callable[..., list] | None = None,
        allow_local_preview: bool | None = None,
        preview_ports: str | Iterable[int] | None = None,
        public_ports: str | Iterable[int] | None = None,
        preview_grant_loader: Callable[[str], dict] | None = None,
    ):
        self.resolver = resolver or socket.getaddrinfo
        self.allow_local_preview = (
            os.environ.get("PAIRPUTER_ALLOW_LOCAL_PREVIEW", "false").lower()
            in {"1", "true", "yes"}
            if allow_local_preview is None else bool(allow_local_preview)
        )
        self.preview_ports = _parse_ports(
            preview_ports if preview_ports is not None
            else os.environ.get("PAIRPUTER_PREVIEW_PORTS", "3000-5899,7000-8999")
        )
        configured_public = _parse_ports(
            public_ports if public_ports is not None
            else os.environ.get("PAIRPUTER_BROWSER_REMOTE_PORTS", "80,443")
        )
        if not configured_public <= PUBLIC_PORTS:
            raise ValueError("public egress ports cannot exceed 80 and 443")
        self.public_ports = configured_public
        self.preview_grant_dir = os.environ.get(
            "PAIRPUTER_PREVIEW_GRANT_DIR", "/run/pairputer/preview-grants"
        )
        self.preview_grant_loader = preview_grant_loader

    def _preview_grant(self, token: str) -> dict:
        if self.preview_grant_loader is not None:
            value = self.preview_grant_loader(token)
        else:
            path = os.path.join(self.preview_grant_dir, token)
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags)
            try:
                stat = os.fstat(fd)
                if (stat.st_uid != 0 or stat.st_gid != os.getegid() or
                        stat.st_mode & 0o777 != 0o640 or stat.st_size > 4096):
                    raise ProxyPolicyDenied("preview grant file is unsafe")
                raw = os.read(fd, 4097)
            finally:
                os.close(fd)
            value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("token") != token or
                int(value.get("expires_at", 0)) <= int(time.time())):
            raise ProxyPolicyDenied("preview grant is missing or expired")
        return value

    @staticmethod
    def _normalize_host(raw: str) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
        value = str(raw).strip().lower().rstrip(".")
        if (not value or len(value) > 253 or any(ord(char) < 33 for char in value)
                or any(char in value for char in "/\\@%#")):
            raise ProxyPolicyDenied("invalid target host")
        try:
            literal = ipaddress.ip_address(value)
            return literal.compressed, literal
        except ValueError:
            pass
        try:
            value = value.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ProxyPolicyDenied("invalid target host") from exc
        labels = value.split(".")
        if (len(labels) < 2 and value != "localhost") or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            for label in labels
        ):
            raise ProxyPolicyDenied("target host must be a fully-qualified DNS name")
        if value in _METADATA_NAMES or value.endswith(".metadata.google.internal"):
            raise ProxyPolicyDenied("metadata targets are forbidden")
        return value, None

    def resolve(self, raw_host: str, raw_port: int) -> ResolvedTarget:
        host, literal = self._normalize_host(raw_host)
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ProxyPolicyDenied("invalid target port") from exc
        if port < 1 or port > 65535 or port in PROTECTED_PORTS:
            raise ProxyPolicyDenied("protected target port")

        preview_match = _PREVIEW_HOST_RE.fullmatch(host)
        if preview_match:
            if not self.allow_local_preview:
                raise ProxyPolicyDenied("localhost preview is disabled")
            grant = self._preview_grant(preview_match.group(1))
            if int(grant.get("port", 0)) != port or port not in self.preview_ports:
                raise ProxyPolicyDenied("preview grant does not match the target port")
            return ResolvedTarget(
                host="localhost", port=port, ip="127.0.0.1", family=socket.AF_INET,
                socktype=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
                sockaddr=("127.0.0.1", port),
            )

        if literal is not None:
            family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
            sockaddr = ((literal.compressed, port, 0, 0) if literal.version == 6
                        else (literal.compressed, port))
            records = [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]
        else:
            try:
                future = _DNS_POOL.submit(
                    self.resolver, host, port, socket.AF_UNSPEC,
                    socket.SOCK_STREAM, socket.IPPROTO_TCP,
                )
                records = future.result(timeout=DNS_TIMEOUT)
            except (socket.gaierror, OSError, FutureTimeout) as exc:
                future.cancel()
                raise ProxyPolicyDenied("target DNS resolution failed") from exc
        if not records:
            raise ProxyPolicyDenied("target DNS resolution returned no addresses")

        candidates: list[tuple[ipaddress._BaseAddress, tuple]] = []
        for record in records:
            if len(record) != 5:
                raise ProxyPolicyDenied("target DNS result is invalid")
            family, socktype, proto, _canonname, sockaddr = record
            if family not in {socket.AF_INET, socket.AF_INET6} or socktype != socket.SOCK_STREAM:
                continue
            try:
                address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
            except (ValueError, IndexError, TypeError) as exc:
                raise ProxyPolicyDenied("target DNS address is invalid") from exc
            candidates.append((address, (family, socktype, proto or socket.IPPROTO_TCP, sockaddr)))
        if not candidates:
            raise ProxyPolicyDenied("target DNS resolution returned no TCP addresses")

        addresses = tuple(item[0] for item in candidates)
        if all(address.is_loopback for address in addresses):
            # Literal localhost is never sufficient: remote redirects can look
            # identical to explicit navigations at the proxy boundary. Only a
            # root-minted synthetic preview origin above may reach loopback.
            raise ProxyPolicyDenied("broker-minted localhost preview grant required")
        else:
            if any(not address.is_global for address in addresses):
                raise ProxyPolicyDenied("target resolves to a non-global address")
            if port not in self.public_ports:
                raise ProxyPolicyDenied("public target port is not allowed")

        address, record = min(candidates, key=lambda item: (item[0].version, item[0].packed))
        family, socktype, proto, sockaddr = record
        # ``sockaddr`` came from the one resolver invocation and contains a
        # numeric address.  No destination hostname is resolved after this.
        return ResolvedTarget(
            host=host, port=port, ip=address.compressed, family=family,
            socktype=socktype, proto=proto, sockaddr=sockaddr,
        )


def _connect_numeric(target: ResolvedTarget) -> socket.socket:
    upstream = socket.socket(target.family, target.socktype, target.proto)
    try:
        upstream.settimeout(CONNECT_TIMEOUT)
        upstream.connect(target.sockaddr)
        upstream.settimeout(IDLE_TIMEOUT)
        return upstream
    except OSError as exc:
        upstream.close()
        raise ProxyUpstreamError("numeric upstream connection failed") from exc


def _relay(
    client: socket.socket,
    upstream: socket.socket,
    *,
    idle_timeout: float = IDLE_TIMEOUT,
    max_lifetime: float = MAX_CONNECTION_LIFETIME,
    max_stream_bytes: int = MAX_STREAM_BYTES,
) -> None:
    # Defaults suit a one-shot HTTP fetch. A long-lived WebSocket tunnel to a loopback preview
    # grant relaxes them (an editor session stays quiet for minutes and outlives 10 min) — safe
    # only because that tunnel can reach ONLY the in-box, broker-authorized preview, never egress.
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    started = last_activity = time.monotonic()
    transferred = {client: 0, upstream: 0}
    try:
        while selector.get_map():
            now = time.monotonic()
            if now - started >= max_lifetime or now - last_activity >= idle_timeout:
                return
            events = selector.select(min(1.0, idle_timeout - (now - last_activity)))
            if not events:
                continue
            for key, _mask in events:
                source: socket.socket = key.fileobj
                destination: socket.socket = key.data
                try:
                    chunk = source.recv(64 * 1024)
                except (ConnectionResetError, OSError):
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(source)
                    except (KeyError, ValueError):
                        pass
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                transferred[source] += len(chunk)
                if transferred[source] > max_stream_bytes:
                    return
                destination.sendall(chunk)
                last_activity = time.monotonic()
    finally:
        selector.close()


class EgressProxyHandler(socketserver.BaseRequestHandler):
    server: "EgressProxyServer"

    def _send_status(self, status: int) -> None:
        phrases = {204: "No Content", 400: "Bad Request", 403: "Forbidden",
                   405: "Method Not Allowed", 431: "Request Header Fields Too Large",
                   502: "Bad Gateway", 503: "Service Unavailable"}
        phrase = phrases.get(status, "Bad Request")
        try:
            self.request.sendall(
                f"HTTP/1.1 {status} {phrase}\r\nContent-Length: 0\r\n"
                "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode("ascii")
            )
        except OSError:
            pass

    def _read_request(self):
        self.request.settimeout(HEADER_TIMEOUT)
        buffer = bytearray()
        while True:
            marker = buffer.find(b"\r\n\r\n")
            if marker >= 0:
                if marker + 4 > MAX_HEADER_BYTES:
                    raise ProxyHeadersTooLarge("request headers exceed limit")
                head = bytes(buffer[:marker])
                extra = bytes(buffer[marker + 4:])
                break
            if len(buffer) >= MAX_HEADER_BYTES:
                raise ProxyHeadersTooLarge("request headers exceed limit")
            chunk = self.request.recv(min(4096, MAX_HEADER_BYTES + 1 - len(buffer)))
            if not chunk:
                raise ProxyRequestError("incomplete request headers")
            buffer.extend(chunk)
        lines = head.split(b"\r\n")
        if not lines or len(lines[0]) > MAX_REQUEST_LINE:
            raise ProxyRequestError("invalid request line")
        try:
            method, target, version = lines[0].decode("ascii", "strict").split(" ")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProxyRequestError("invalid request line") from exc
        if not _METHOD.fullmatch(method) or version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ProxyRequestError("invalid request method or version")
        if len(lines) - 1 > MAX_HEADER_COUNT:
            raise ProxyHeadersTooLarge("too many request headers")
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
                raise ProxyRequestError("invalid request header")
            name, value = line.split(b":", 1)
            if not _HEADER_NAME.fullmatch(name) or b"\x00" in value:
                raise ProxyRequestError("invalid request header")
            value = value.strip()
            if any(byte < 32 and byte != 9 for byte in value):
                raise ProxyRequestError("invalid request header value")
            headers.append((name.decode("ascii").lower(), value.decode("latin-1")))
        return method, target, headers, extra

    @staticmethod
    def _authority(raw: str, default_port: int | None = None) -> tuple[str, int]:
        try:
            parsed = urllib.parse.urlsplit("//" + raw)
            if (not parsed.hostname or parsed.username or parsed.password or parsed.path
                    or parsed.query or parsed.fragment):
                raise ValueError
            port = parsed.port if parsed.port is not None else default_port
        except ValueError as exc:
            raise ProxyRequestError("invalid target authority") from exc
        if port is None:
            raise ProxyRequestError("target authority requires a port")
        return parsed.hostname, port

    def _connect(self, raw_authority: str, extra: bytes) -> None:
        host, port = self._authority(raw_authority)
        target = self.server.policy.resolve(host, port)
        if ipaddress.ip_address(target.ip).is_loopback:
            # CONNECT hides the browser's Fetch Metadata inside TLS, so the
            # proxy cannot distinguish an explicit preview from a hostile
            # remote redirect. Local previews are deliberately HTTP-only.
            raise ProxyPolicyDenied("TLS tunnels to localhost previews are forbidden")
        with _connect_numeric(target) as upstream:
            self.request.sendall(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: Pairputer-Egress\r\n\r\n"
            )
            if extra:
                upstream.sendall(extra)
            _relay(self.request, upstream)

    def _forward(self, method: str, raw_target: str,
                 headers: list[tuple[str, str]], extra: bytes) -> None:
        if method not in _FORWARD_METHODS:
            error = ProxyRequestError("forward method is not allowed")
            error.status = 405
            raise error
        try:
            parsed = urllib.parse.urlsplit(raw_target)
            if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.fragment):
                raise ValueError
            port = parsed.port or 80
        except ValueError as exc:
            raise ProxyRequestError("forward proxy requires an absolute HTTP URL") from exc
        target = self.server.policy.resolve(parsed.hostname, port)
        host_value = target.host
        if ":" in host_value:
            host_value = f"[{host_value}]"
        if port != 80:
            host_value += f":{port}"
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))

        content_lengths = [value for name, value in headers if name == "content-length"]
        transfer_encodings = [value for name, value in headers if name == "transfer-encoding"]
        if (len(content_lengths) > 1 or len(transfer_encodings) > 1
                or (content_lengths and transfer_encodings)):
            raise ProxyRequestError("ambiguous request body framing")
        if content_lengths:
            try:
                length = int(content_lengths[0])
            except ValueError as exc:
                raise ProxyRequestError("invalid content length") from exc
            if length < 0 or length > MAX_STREAM_BYTES:
                raise ProxyRequestError("request body exceeds limit")
        if transfer_encodings and transfer_encodings[0].strip().lower() != "chunked":
            raise ProxyRequestError("unsupported transfer encoding")

        # WebSocket upgrades are cleartext protocol switches, so they are refused for every REAL
        # egress destination — a page must not tunnel an arbitrary framed protocol off-box. The
        # ONE exception is the loopback preview grant: ``resolve()`` returns ip==127.0.0.1 solely
        # for a root-minted ``p-<hash>.pairputer-preview.invalid`` host whose grant + port it already
        # validated, so an upgrade here can only reach an in-box preview the broker authorized
        # (code-server/VS Code, HMR dev servers). It never touches the internet. Anything that isn't
        # a clean ``Upgrade: websocket`` + ``Connection: upgrade`` to that loopback target stays
        # rejected. GET-only, matching the WebSocket handshake (no body framing to smuggle).
        upgrade_values = [value.strip().lower() for name, value in headers if name == "upgrade"]
        connection_tokens = {
            token.strip().lower()
            for name, value in headers if name == "connection"
            for token in value.split(",")
        }
        is_ws_upgrade = (
            upgrade_values == ["websocket"]
            and "upgrade" in connection_tokens
            and method == "GET"
            and not content_lengths and not transfer_encodings
        )
        target_is_loopback_preview = ipaddress.ip_address(target.ip).is_loopback
        if upgrade_values:
            if not (is_ws_upgrade and target_is_loopback_preview):
                raise ProxyRequestError("cleartext protocol upgrades are not supported")

        stripped = {"host", "connection", "proxy-connection", "keep-alive",
                    "proxy-authorization", "proxy-authenticate", "te", "trailer", "upgrade"}
        output = [f"{method} {path} HTTP/1.1\r\n", f"Host: {host_value}\r\n"]
        for name, value in headers:
            if name not in stripped:
                output.append(f"{name}: {value}\r\n")
        if is_ws_upgrade and target_is_loopback_preview:
            # Carry the handshake through verbatim and keep the connection open so ``_relay`` can
            # tunnel frames both ways. Only reachable for the validated loopback preview grant above.
            output.append("Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
        else:
            output.append("Connection: close\r\n\r\n")
        encoded = "".join(output).encode("latin-1")
        if len(encoded) > MAX_HEADER_BYTES:
            raise ProxyHeadersTooLarge("rewritten headers exceed limit")
        with _connect_numeric(target) as upstream:
            upstream.sendall(encoded)
            if extra:
                upstream.sendall(extra)
            # Drop the 5s header-read deadline left on the client socket; _relay is selector-driven,
            # so a lingering timeout would only fire spuriously on a blocked sendall mid-stream.
            self.request.settimeout(None)
            if is_ws_upgrade and target_is_loopback_preview:
                _relay(self.request, upstream, idle_timeout=WS_IDLE_TIMEOUT,
                       max_lifetime=WS_MAX_LIFETIME, max_stream_bytes=WS_MAX_STREAM_BYTES)
            else:
                _relay(self.request, upstream)

    def handle(self) -> None:
        try:
            method, target, headers, extra = self._read_request()
            if method == "GET" and target == "/health":
                self._send_status(204)
                return
            if method == "CONNECT":
                self._connect(target, extra)
            else:
                self._forward(method, target, headers, extra)
        except ProxyRequestError as exc:
            self._send_status(getattr(exc, "status", 400))
        except (ProxyUpstreamError, socket.timeout, OSError):
            self._send_status(502)


class EgressProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address=(LISTEN_HOST, LISTEN_PORT), *, policy: EgressPolicy | None = None):
        if ipaddress.ip_address(address[0]).is_loopback is not True:
            raise ValueError("egress proxy must bind loopback")
        self.policy = policy or EgressPolicy()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        super().__init__(address, EgressProxyHandler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
            except OSError:
                pass
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # Never let exception formatting disclose a URL or request header.
        return


def serve() -> None:
    with EgressProxyServer() as server:
        print(f"[egress_proxy] loopback proxy ready on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
        server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    serve()
