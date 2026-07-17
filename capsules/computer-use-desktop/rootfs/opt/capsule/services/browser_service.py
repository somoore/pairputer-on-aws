from __future__ import annotations

import ipaddress
import grp
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from .common import action_result, evidence, require_action_envelope
from .control_state import LeaseRejected


BROWSER_QUERY_FIELDS = frozenset({"task_id", "tab_id", "selector"})
_SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?:pass(?:word|code|phrase)?|otp|one[-_ ]?time|token|secret|api[-_ ]?key|auth(?:orization)?|"
    r"bearer|session|csrf|credential|pin|ssn|social[-_ ]?security|card|cc[-_ ]?(?:number|csc|cvv)|cvc|cvv)",
    re.IGNORECASE,
)
_SENSITIVE_AUTOCOMPLETE = frozenset({
    "username", "current-password", "new-password", "one-time-code",
    "cc-name", "cc-given-name", "cc-additional-name", "cc-family-name",
    "cc-number", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-csc", "cc-type",
    "transaction-currency", "transaction-amount", "bday", "bday-day", "bday-month",
    "bday-year", "tel", "tel-country-code", "tel-national", "tel-area-code",
    "tel-local", "tel-local-prefix", "tel-local-suffix", "tel-extension", "email",
    "street-address", "address-line1", "address-line2", "address-line3", "postal-code",
})
_AX_VALUE_CONTROL_ROLES = frozenset({
    "combobox", "listbox", "searchbox", "slider", "spinbutton", "textbox",
})
_PREVIEW_HOST_RE = re.compile(r"^p-([a-f0-9]{43})\.pairputer-preview\.invalid$")
_TRANSIENT_QUERY_ERRORS = (
    "browser document did not resolve",
    "browser selector did not resolve",
    "browser selector has no accessibility node",
    "browser selector does not resolve to semantic visible content",
    "no target with given id",
    "target closed",
    "timed out",
)


def _is_timeout_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError) or "timed out" in str(current).casefold():
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_transient_query_error(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    message = str(error).casefold()
    return any(marker in message for marker in _TRANSIENT_QUERY_ERRORS)


def strict_browser_query_request(request: dict) -> dict:
    """Enforce the manifest's closed schema before adding the fixed internal action.

    ``capsule_invoke`` intentionally accepts a compatibility ``args`` object, so FastMCP cannot enforce
    ``additionalProperties: false`` at that outer boundary. The bridge therefore rejects action, value,
    and envelope fields rather than silently discarding them.
    """
    if not isinstance(request, dict):
        raise ValueError("browser query requires an object")
    unknown = set(request) - BROWSER_QUERY_FIELDS
    if unknown:
        raise ValueError("unknown browser query fields: " + ", ".join(sorted(unknown)))
    result = dict(request)
    result["browser_action"] = "query"
    return result


def _ax_value(field) -> str:
    if not isinstance(field, dict):
        return ""
    value = field.get("value", "")
    return value if isinstance(value, str) else str(value)


def _node_attributes(node: dict) -> dict[str, str]:
    raw = node.get("attributes") or []
    if not isinstance(raw, list):
        return {}
    return {
        str(raw[index]).lower(): str(raw[index + 1])
        for index in range(0, len(raw) - 1, 2)
    }


def _assert_query_safe_node(node: dict) -> None:
    """Refuse selectors that directly target hidden or credential-like controls."""
    tag = str(node.get("nodeName", "")).lower()
    attributes = _node_attributes(node)
    input_type = attributes.get("type", "text").strip().lower() if tag == "input" else ""
    if "hidden" in attributes or attributes.get("aria-hidden", "").strip().lower() == "true":
        raise ValueError("browser selector resolves to hidden content")
    if tag == "input" and input_type in {"password", "hidden", "file"}:
        raise ValueError("browser selector resolves to a protected input field")
    autocomplete = {part.lower() for part in attributes.get("autocomplete", "").split()}
    if autocomplete & _SENSITIVE_AUTOCOMPLETE:
        raise ValueError("browser selector resolves to an autocomplete-sensitive field")
    identity = " ".join(attributes.get(key, "") for key in (
        "id", "name", "aria-label", "placeholder", "data-testid",
    ))
    if _SENSITIVE_FIELD_PATTERN.search(identity):
        raise ValueError("browser selector resolves to a credential-like field")


def _bounded_ax_text(nodes: list, limit: int = 8192,
                     allowed_node_ids: set[str] | None = None) -> str:
    """Extract AX names without form-value fields or their browser shadow-DOM descendants."""
    values = nodes if isinstance(nodes, list) else []
    blocked = {
        str(node.get("nodeId")) for node in values
        if isinstance(node, dict) and node.get("nodeId") is not None and
        _ax_value(node.get("role")).lower() in _AX_VALUE_CONTROL_ROLES
    }
    # Every live form-control value is sensitive, irrespective of its label.
    # Sites routinely use generic names such as "Code" or no accessible name
    # at all, and can splice that value into another node's computed name.
    sensitive_controls = set(blocked)
    # Chromium represents an input's live value as StaticText/InlineTextBox descendants beneath the
    # control. Ignoring only AXNode.value is therefore insufficient; exclude the complete value-control
    # subtree as well.
    changed = True
    while changed:
        changed = False
        for node in values:
            if not isinstance(node, dict) or node.get("nodeId") is None:
                continue
            node_id = str(node["nodeId"])
            if node_id not in blocked and str(node.get("parentId", "")) in blocked:
                blocked.add(node_id)
                changed = True
    blocked_backend = {
        str(node.get("backendDOMNodeId")) for node in values
        if isinstance(node, dict) and str(node.get("nodeId")) in sensitive_controls and
        node.get("backendDOMNodeId") is not None
    }
    # A computed accessible name can copy the live value of a different form
    # control (for example aria-labelledby="label-containing-otp-input").
    # Taint both explicit related-node sources and any computed name that
    # contains a blocked control/subtree value.  We never emit the source/value
    # fields themselves.
    sensitive_values: set[str] = set()
    for node in values:
        if not isinstance(node, dict) or str(node.get("nodeId")) not in sensitive_controls:
            continue
        for field in (node.get("value"), node.get("name")):
            candidate = " ".join(_ax_value(field).split()).casefold()
            if candidate:
                sensitive_values.add(candidate)

    def tainted_name(node: dict, candidate: str) -> bool:
        folded = candidate.casefold()
        if any(secret == folded or (len(secret) >= 3 and secret in folded)
               for secret in sensitive_values):
            return True
        name = node.get("name")
        sources = name.get("sources") if isinstance(name, dict) else None

        def source_tainted(value) -> bool:
            if isinstance(value, dict):
                backend = value.get("backendDOMNodeId")
                if backend is not None and str(backend) in blocked_backend:
                    return True
                return any(source_tainted(item) for item in value.values())
            if isinstance(value, list):
                return any(source_tainted(item) for item in value)
            if isinstance(value, str):
                text = " ".join(value.split()).casefold()
                return bool(text) and any(
                    secret == text or (len(secret) >= 3 and secret in text)
                    for secret in sensitive_values
                )
            return False

        for source in sources if isinstance(sources, list) else ():
            if source_tainted(source):
                return True
        return False
    parts: list[str] = []
    length = 0
    previous = ""
    for node in values:
        if not isinstance(node, dict) or node.get("ignored") is True:
            continue
        if allowed_node_ids is not None and str(node.get("nodeId")) not in allowed_node_ids:
            continue
        if (str(node.get("nodeId")) in blocked or
                _ax_value(node.get("role")).lower() in _AX_VALUE_CONTROL_ROLES):
            continue
        value = " ".join(_ax_value(node.get("name")).split())
        if not value or value == previous or tainted_name(node, value):
            continue
        remaining = limit - length
        if remaining <= 0:
            break
        value = value[:remaining]
        parts.append(value)
        length += len(value) + 1
        previous = value
    return " ".join(parts)[:limit]


class TaskDomainGrantStore:
    """Server-side, active-task domain grants under a deployment ceiling."""

    _TASK_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

    def __init__(self, deployment_ceiling=None, *, resolver=None,
                 allow_local_preview=None, preview_ports=None, remote_ports=None):
        raw = deployment_ceiling if deployment_ceiling is not None else os.environ.get(
            "PAIRPUTER_BROWSER_DOMAIN_CEILING", "*"
        )
        values = raw.split(",") if isinstance(raw, str) else raw
        self.deployment_ceiling = frozenset(
            self._normalize_domain(item, allow_wildcard=True) for item in values if str(item).strip()
        )
        if not self.deployment_ceiling:
            raise ValueError("browser deployment domain ceiling must not be empty")
        self._resolver = resolver or self._system_resolver
        self.allow_local_preview = (
            str(os.environ.get("PAIRPUTER_ALLOW_LOCAL_PREVIEW", "false")).lower() in {"1", "true", "yes"}
            if allow_local_preview is None else bool(allow_local_preview)
        )
        self.preview_ports = self._port_policy(
            preview_ports if preview_ports is not None else
            os.environ.get("PAIRPUTER_PREVIEW_PORTS", "3000-5899,7000-8999")
        )
        self.remote_ports = self._port_policy(
            remote_ports if remote_ports is not None else
            os.environ.get("PAIRPUTER_BROWSER_REMOTE_PORTS", "80,443")
        )
        self.grant_ttl_seconds = max(60, min(int(os.environ.get(
            "PAIRPUTER_TASK_DOMAIN_GRANT_TTL_SECONDS", "3600"
        )), 24 * 3600))
        self._grants: dict[str, tuple[frozenset[str], float]] = {}
        self._revoke_callbacks = []
        self._lock = threading.RLock()

    def add_revoke_callback(self, callback) -> None:
        self._revoke_callbacks.append(callback)

    @staticmethod
    def _system_resolver(host: str) -> tuple[str, ...]:
        try:
            return tuple(sorted({item[4][0] for item in socket.getaddrinfo(
                host, None, type=socket.SOCK_STREAM
            )}))
        except socket.gaierror as exc:
            raise ValueError("browser host DNS resolution failed") from exc

    @staticmethod
    def _port_policy(raw) -> frozenset[int]:
        values = raw.split(",") if isinstance(raw, str) else raw
        ports: set[int] = set()
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
                raise ValueError("invalid browser port policy") from exc
            if first < 1 or last > 65535 or first > last or last - first > 10000:
                raise ValueError("invalid browser port policy")
            ports.update(range(first, last + 1))
        if not ports:
            raise ValueError("browser port policy must not be empty")
        return frozenset(ports)

    @staticmethod
    def _normalize_domain(raw, *, allow_wildcard=False) -> str:
        value = str(raw).strip().lower().rstrip(".")
        if allow_wildcard and value == "*":
            return value
        if (not value or value == "*" or "://" in value or "/" in value or "@" in value or
                len(value) > 253):
            raise ValueError("invalid browser policy domain")
        try:
            return ipaddress.ip_address(value).compressed
        except ValueError:
            pass
        try:
            value = value.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("invalid browser policy domain") from exc
        if any(not label or len(label) > 63 for label in value.split(".")):
            raise ValueError("invalid browser policy domain")
        if "." not in value and value != "localhost":
            raise ValueError("remote browser policy domains must be fully qualified")
        return value

    @staticmethod
    def _contains(scope: frozenset[str], host: str) -> bool:
        return "*" in scope or any(host == domain or host.endswith("." + domain) for domain in scope)

    def register(self, task_id: str, allowed_domains) -> dict:
        task_id = str(task_id)
        if not self._TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task domain grant id")
        values = tuple(allowed_domains or ())
        if len(values) > 100:
            raise ValueError("task domain grant exceeds 100 domains")
        grant = frozenset(self._normalize_domain(item) for item in values)
        outside = sorted(domain for domain in grant
                         if not self._contains(self.deployment_ceiling, domain))
        if outside:
            raise ValueError("task domain grant exceeds the deployment ceiling")
        with self._lock:
            existing = self._grants.get(task_id)
            if existing is not None and existing[0] != grant:
                raise ValueError("active task domain grant cannot be widened or replaced")
            expires_at = time.time() + self.grant_ttl_seconds
            self._grants[task_id] = (grant, expires_at)
        return {"taskId": task_id, "allowedDomains": sorted(grant), "active": True,
                "expiresAt": expires_at}

    def revoke(self, task_id: str) -> dict:
        with self._lock:
            existed = self._grants.pop(str(task_id), None) is not None
        for callback in tuple(self._revoke_callbacks):
            callback(str(task_id))
        return {"taskId": str(task_id), "active": False, "revoked": existed}

    def clear(self) -> dict:
        with self._lock:
            count = len(self._grants)
            self._grants.clear()
        for callback in tuple(self._revoke_callbacks):
            callback(None)
        return {"active": False, "revokedCount": count}

    def active(self, task_id: str) -> bool:
        with self._lock:
            task_id = str(task_id or "")
            item = self._grants.get(task_id)
            if item is None:
                return False
            if item[1] <= time.time():
                self._grants.pop(task_id, None)
                return False
            return True

    _WORKSPACE_ROOT = os.path.realpath(os.environ.get("PAIRPUTER_WORKSPACE", "/home/app/workspace"))

    def _authorize_workspace_file(self, parsed) -> str:
        """Authorize a file:// URL ONLY when it resolves inside the confined workspace subtree.
        Traversal-safe: the path is realpath-resolved and must stay under the workspace root, so
        ../ / symlink escapes are rejected. A file:// with a non-empty host (file://host/...) is
        refused — only local file://[/]path is allowed."""
        if parsed.netloc not in ("", "localhost"):
            raise ValueError("file:// URLs may not name a remote host")
        path = urllib.parse.unquote(parsed.path or "")
        if not path.startswith("/"):
            raise ValueError("file:// path must be absolute")
        real = os.path.realpath(path)
        root = self._WORKSPACE_ROOT
        if real != root and not real.startswith(root + os.sep):
            raise ValueError("file:// is confined to the workspace; that path is outside it")
        # canonical, traversal-free file URL
        return "file://" + urllib.parse.quote(real)

    def authorize_url(self, task_id: str, raw) -> str:
        task_id = str(task_id or "")
        # Confined file:// FIRST — a host that authored a page in the sandbox (workspace_write) can open
        # it directly. It is domain-free and workspace-confined, so the task-domain-grant / SSRF / DNS
        # machinery below doesn't apply. ONLY the workspace subtree is allowed; arbitrary file:// (and
        # every other non-HTTP(S) scheme) stays blocked, and ../ / symlink escapes are realpath-rejected.
        _pre = urllib.parse.urlparse(str(raw))
        if _pre.scheme == "file":
            return self._authorize_workspace_file(_pre)
        # Autonomy: navigation is an in-VM effect (loading a page in a disposable VM). Skip the
        # per-task domain GRANT requirement — that friction isn't worth it here — but KEEP every
        # safety check below: credential-free URL, deployment ceiling, and the SSRF/IP guards
        # (link-local, loopback, private ranges). The external-commit gate still catches submit/
        # upload/etc. So: browse anywhere on the public internet, but can't be tricked into the
        # metadata endpoint or an internal host, and can't silently POST a form off-box.
        autonomy = os.environ.get("PAIRPUTER_WORKBENCH_AUTONOMY", "").lower() in {"1", "true", "yes", "on"}
        if not autonomy:
            with self._lock:
                item = self._grants.get(task_id)
                if item is not None and item[1] <= time.time():
                    self._grants.pop(task_id, None)
                    item = None
            if item is None:
                print("[browser-policy] active task grant missing", {
                    "task": task_id[:24], "active_grants": len(self._grants),
                }, flush=True)
                raise ValueError("browser operation requires an active task domain grant")
            grant = item[0]
        else:
            grant = None
        parsed = _pre
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("browser URL must be credential-free HTTP(S)")
        host = self._normalize_domain(parsed.hostname)
        if grant is not None and not self._contains(grant, host):
            raise ValueError("browser domain is outside the active task grant")
        if not self._contains(self.deployment_ceiling, host):
            raise ValueError("browser domain is outside the deployment ceiling")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise ValueError("browser URL port is invalid") from exc
        try:
            literal = ipaddress.ip_address(host)
            addresses = (literal,)
        except ValueError:
            raw_addresses = self._resolver(host)
            if not raw_addresses:
                raise ValueError("browser host DNS resolution returned no addresses")
            try:
                addresses = tuple(ipaddress.ip_address(item) for item in raw_addresses)
            except ValueError as exc:
                raise ValueError("browser host DNS resolution returned an invalid address") from exc
        loopback = all(address.is_loopback for address in addresses)
        if loopback:
            if parsed.scheme != "http":
                raise ValueError("localhost previews are HTTP-only")
            if not self.allow_local_preview or port not in self.preview_ports:
                raise ValueError("localhost preview is not enabled for this port")
        else:
            if any(not address.is_global for address in addresses):
                raise ValueError("browser target resolves to a non-global network address")
            if port not in self.remote_ports:
                raise ValueError("browser remote port is outside deployment policy")
        return parsed.geturl()


class PreviewGrantStore:
    """Root-minted, task-bound capabilities for exact localhost preview ports."""

    def __init__(self, directory=None, ttl_seconds=600):
        self.directory = Path(directory or os.environ.get(
            "PAIRPUTER_PREVIEW_GRANT_DIR", "/run/pairputer/preview-grants"
        ))
        self.ttl_seconds = max(30, min(int(ttl_seconds), 3600))
        self._records: dict[str, dict] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _loopback_url(raw: str) -> tuple[urllib.parse.ParseResult, int] | None:
        parsed = urllib.parse.urlparse(str(raw))
        if parsed.scheme != "http" or not parsed.hostname:
            return None
        try:
            address = ipaddress.ip_address(parsed.hostname)
            loopback = address.is_loopback
        except ValueError:
            loopback = parsed.hostname.lower().rstrip(".") == "localhost"
        return (parsed, parsed.port or 80) if loopback else None

    def issue(self, task_id: str, raw_url: str) -> str:
        target = self._loopback_url(raw_url)
        if target is None:
            return raw_url
        parsed, port = target
        # DNS hostnames are case-insensitive and Chromium canonicalizes them to
        # lowercase. Use a lowercase-only 172-bit capability so the authority
        # survives URL normalization byte-for-byte.
        token = secrets.token_hex(22)[:43]
        expires_at = int(time.time()) + self.ttl_seconds
        record = {
            "token": token, "task_id": str(task_id), "port": port,
            "original_host": parsed.hostname, "expires_at": expires_at,
        }
        path = self.directory / token
        try:
            directory_stat = self.directory.stat(follow_symlinks=False)
            expected_gid = grp.getgrnam("egressd").gr_gid
            if (directory_stat.st_uid != 0 or directory_stat.st_gid != expected_gid or
                    directory_stat.st_mode & 0o027):
                raise PermissionError("preview grant directory is unsafe")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, 0o640)
            try:
                os.fchmod(fd, 0o640)
                os.fchown(fd, 0, expected_gid)
                payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        with self._lock:
            self._records[token] = record
        netloc = f"p-{token}.pairputer-preview.invalid:{port}"
        return urllib.parse.urlunparse(("http", netloc, parsed.path or "/", parsed.params,
                                       parsed.query, ""))

    def original_url(self, task_id: str, raw_url: str) -> str | None:
        parsed = urllib.parse.urlparse(str(raw_url))
        match = _PREVIEW_HOST_RE.fullmatch((parsed.hostname or "").lower())
        if not match:
            return None
        with self._lock:
            record = self._records.get(match.group(1))
        if (not record or record["task_id"] != str(task_id) or
                int(record["expires_at"]) <= int(time.time())):
            raise ValueError("localhost preview grant is missing or expired")
        host = str(record["original_host"])
        netloc = f"[{host}]" if ":" in host else host
        if int(record["port"]) != 80:
            netloc += f":{int(record['port'])}"
        return urllib.parse.urlunparse(("http", netloc, parsed.path or "/", parsed.params,
                                       parsed.query, ""))

    def revoke(self, task_id: str | None) -> None:
        with self._lock:
            tokens = [token for token, record in self._records.items()
                      if task_id is None or record["task_id"] == str(task_id)]
            for token in tokens:
                self._records.pop(token, None)
                try:
                    (self.directory / token).unlink()
                except FileNotFoundError:
                    pass


class BrowserService:
    # Client-rendered commerce/developer pages often install their semantic main region after the
    # navigation receipt. These are read-only retries with a fresh domain check before each attempt.
    _QUERY_RETRY_DELAYS = (0.25, 0.75, 1.5, 2.5)

    def __init__(self, control, cdp, allowed_domains=None, task_grants=None, preview_grants=None):
        self.control, self.cdp = control, cdp
        raw = allowed_domains if allowed_domains is not None else os.environ.get("PAIRPUTER_ALLOWED_DOMAINS", "localhost,127.0.0.1")
        self.allowed_domains = {item.strip().lower() for item in (raw.split(",") if isinstance(raw, str) else raw) if item.strip()}
        self.task_grants = task_grants or TaskDomainGrantStore(
            allowed_domains if allowed_domains is not None else None
        )
        self.preview_grants = preview_grants or PreviewGrantStore()
        self.task_grants.add_revoke_callback(self.preview_grants.revoke)

    def _url(self, raw, task_id):
        return self.task_grants.authorize_url(task_id, raw)

    def _current_url(self, raw, task_id):
        original = self.preview_grants.original_url(task_id, raw)
        return self._url(original or raw, task_id)

    def _network_url(self, raw, task_id):
        return self.preview_grants.issue(task_id, raw)

    @staticmethod
    def _output_url(raw):
        """Return origin/path provenance without query-string or fragment credentials."""
        parsed = urllib.parse.urlparse(str(raw))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return (parsed.scheme[:32] + ":") if parsed.scheme else ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = host + (f":{port}" if port is not None else "")
        return urllib.parse.urlunparse((parsed.scheme, netloc, parsed.path or "/", "", "", ""))[:4096]

    def observe(self, request=None):
        request = request or {}
        task_id = str(request.get("task_id") or "")
        autonomy = os.environ.get("PAIRPUTER_WORKBENCH_AUTONOMY", "").lower() in {"1", "true", "yes", "on"}
        if not autonomy and not self.task_grants.active(task_id):
            raise ValueError("browser observation requires an active task domain grant")
        tabs = [tab for tab in self.cdp.tabs()
                if self._tab_authorized(task_id, tab.get("url", ""))]
        # Page titles are attacker-controlled and can mirror form values. Keep
        # tab discovery metadata-only; semantic content must use the protected
        # DOM/AX query path.
        return {"ok": True, "tabs": [{
            "id": tab.get("id", ""),
            "url": self._output_url(self._current_url(tab.get("url", ""), task_id)),
            "provenance": {"source": "web_page", "trust": "untrusted"},
        } for tab in tabs], **self.control.snapshot()}

    def _tab_authorized(self, task_id, url) -> bool:
        try:
            self._current_url(url, task_id)
            return True
        except ValueError:
            return False

    @staticmethod
    def _resolve_node(session, selector: str) -> tuple[int, dict]:
        document = session.command("DOM.getDocument", {"depth": 0, "pierce": True})
        root_id = int((document.get("root") or {}).get("nodeId") or 0)
        if root_id <= 0:
            raise ValueError("browser document did not resolve")
        match = session.command("DOM.querySelector", {"nodeId": root_id, "selector": selector})
        node_id = int(match.get("nodeId") or 0)
        if node_id <= 0:
            raise ValueError("browser selector did not resolve")
        described = session.command("DOM.describeNode", {
            "nodeId": node_id, "depth": 0, "pierce": True,
        })
        node = described.get("node") or {}
        if not isinstance(node, dict) or not node.get("nodeName"):
            raise ValueError("browser selector did not resolve")
        _assert_query_safe_node(node)
        return node_id, node

    def _query(self, tab: dict, selector: str) -> dict:
        """Read through CDP DOM/Accessibility domains without executing page JavaScript."""
        websocket = tab["webSocketDebuggerUrl"]
        with self.cdp.session(websocket) as session:
            node_id, node = self._resolve_node(session, selector)
            # Analyze the full document tree so a narrow selector cannot hide a
            # referenced form control whose live value Chromium copied into a
            # sibling's computed accessible name. Output remains limited to
            # the selected backend-DOM node's AX subtree.
            tree = session.command("Accessibility.getFullAXTree", {})
            nodes = tree.get("nodes") or []
            backend_id = str(node.get("backendNodeId") or "")
            roots = [item for item in nodes if isinstance(item, dict) and
                     str(item.get("backendDOMNodeId") or "") == backend_id]
            if not roots:
                raise ValueError("browser selector has no accessibility node")
            by_id = {str(item.get("nodeId")): item for item in nodes
                     if isinstance(item, dict) and item.get("nodeId") is not None}
            selected: set[str] = set()
            pending = [str(item["nodeId"]) for item in roots if item.get("nodeId") is not None]
            while pending and len(selected) <= 10000:
                current = pending.pop()
                if current in selected:
                    continue
                selected.add(current)
                value = by_id.get(current) or {}
                pending.extend(str(child) for child in (value.get("childIds") or ()))
            if len(selected) > 10000:
                raise ValueError("browser accessibility subtree exceeds the node limit")
        selected_nodes = [item for item in nodes if isinstance(item, dict) and
                          str(item.get("nodeId")) in selected]
        if selected_nodes and all(item.get("ignored") is True for item in selected_nodes):
            raise ValueError("browser selector does not resolve to semantic visible content")
        return {"text": _bounded_ax_text(nodes, allowed_node_ids=selected),
                "tag": str(node["nodeName"]).upper()}

    def _query_with_readiness_retry(self, tab: dict, selector: str, task_id: str) -> dict:
        """Retry only a bounded, read-only semantic observation while a page settles."""

        current = tab
        for attempt in range(len(self._QUERY_RETRY_DELAYS) + 1):
            try:
                return self._query(current, selector)
            except Exception as exc:
                if attempt >= len(self._QUERY_RETRY_DELAYS) or not _is_transient_query_error(exc):
                    raise
                time.sleep(self._QUERY_RETRY_DELAYS[attempt])
                refreshed = next(
                    (item for item in self.cdp.tabs() if item.get("id") == tab.get("id")),
                    None,
                )
                if refreshed is None:
                    raise exc
                # Re-check the active task grant after every renderer refresh. A redirect outside
                # policy must fail immediately rather than being treated as render readiness.
                self._current_url(refreshed.get("url", ""), task_id)
                current = refreshed

    @staticmethod
    def _screen_target(session, page_x=None, page_y=None):
        """Best-effort SCREEN coordinates for visible presentation: the bridge glides the REAL
        cursor here (hybrid mode) so semantic actions are watchable — the halo/attribution ride the
        normal agent-input path. Page->screen via the window's own geometry. Never raises."""
        # Protocol-level geometry ONLY (Browser/Page domains) — never Runtime.evaluate: semantic
        # effects must not execute page JavaScript (a hostile page could tamper with the result).
        try:
            bounds = (session.command("Browser.getWindowForTarget", {}) or {}).get("bounds") or {}
            metrics = session.command("Page.getLayoutMetrics", {}) or {}
            viewport = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
            left, top_px = float(bounds["left"]), float(bounds["top"])
            bw, bh = float(bounds["width"]), float(bounds["height"])
            cw, ch = float(viewport["clientWidth"]), float(viewport["clientHeight"])
            side = max(0.0, (bw - cw) / 2)
            chrome = max(0.0, bh - ch - side)
            if page_x is None:  # navigation has no element: aim at the URL bar
                return {"x": round(left + bw * 0.4), "y": round(top_px + max(40.0, chrome * 0.6))}
            return {"x": round(left + side + float(page_x)),
                    "y": round(top_px + chrome + float(page_y))}
        except Exception:
            return None

    @staticmethod
    def _click_node(session, node_id: int) -> dict:
        session.command("DOM.scrollIntoViewIfNeeded", {"nodeId": node_id})
        model = session.command("DOM.getBoxModel", {"nodeId": node_id}).get("model") or {}
        quad = model.get("border") or model.get("content") or []
        if not isinstance(quad, list) or len(quad) != 8:
            raise ValueError("browser selector has no clickable box")
        x = sum(float(quad[index]) for index in (0, 2, 4, 6)) / 4
        y = sum(float(quad[index]) for index in (1, 3, 5, 7)) / 4
        session.command("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        session.command("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1,
        })
        session.command("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1,
        })
        return {"clicked": True, "x": round(x, 2), "y": round(y, 2)}

    @staticmethod
    def _fill_node(session, node_id: int, value: str) -> dict:
        session.command("DOM.focus", {"nodeId": node_id})
        session.command("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Control", "code": "ControlLeft",
            "windowsVirtualKeyCode": 17, "modifiers": 2,
        })
        session.command("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "a", "code": "KeyA",
            "windowsVirtualKeyCode": 65, "modifiers": 2,
        })
        session.command("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "a", "code": "KeyA",
            "windowsVirtualKeyCode": 65, "modifiers": 2,
        })
        session.command("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Control", "code": "ControlLeft",
            "windowsVirtualKeyCode": 17,
        })
        session.command("Input.insertText", {"text": value})
        return {"filled": True, "insertedCharacters": len(value)}

    @staticmethod
    def _focus_node(session, node_id: int) -> dict:
        session.command("DOM.scrollIntoViewIfNeeded", {"nodeId": node_id})
        session.command("DOM.focus", {"nodeId": node_id})
        return {"focused": True}

    def _ensure_browser(self, timeout: float = 12.0) -> bool:
        """Launch the browser if CDP is down, and report whether CDP is READY. The browser is
        intentionally NOT running until a human or the model opens it (it must never auto-open at
        boot), so 'open this URL' has to be able to start it. Chromium's CDP cold-start under
        software-GL can exceed the tool-call budget, so we return False rather than block forever —
        open() then returns a clean 'browser starting, retry' instead of an opaque connection-refused.
        Returns True if CDP is reachable now."""
        try:
            self.cdp.tabs()
            return True
        except Exception:
            pass
        from .app_service import browser_launch_argv
        try:
            subprocess.Popen(browser_launch_argv(),
                             env={"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C.UTF-8"},
                             stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        except Exception:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.cdp.tabs()
                return True
            except Exception:
                time.sleep(0.3)
        return False

    def open(self, request):
        action_id, epoch, revision, _ = require_action_envelope(request)
        task_id = str(request.get("task_id", ""))
        url = self._url(request.get("url", ""), task_id)
        network_url = self._network_url(url, task_id)
        output_url = self._output_url(url)
        if not self._ensure_browser():
            # Cold-started the browser but CDP isn't ready within the budget — a retryable outcome,
            # not a failure. The retry lands on a now-warm browser and succeeds. retrySafety=safe so
            # the model knows to just call again.
            return action_result(
                accepted=False, action_id=action_id, state=self.control.snapshot(),
                actuator="browser", summary="browser is starting — retry in a moment",
                reason="browser_starting", retry_safety="safe")
        try:
            with self.control.commit(epoch, revision) as state:
                before_ids = {
                    str(item.get("id")) for item in self.cdp.tabs() if item.get("id") is not None
                }
                recovered_unknown_outcome = False
                try:
                    tab = self.cdp.new_tab(network_url)
                except Exception as exc:
                    if not _is_timeout_error(exc):
                        raise
                    # /json/new may create the target and then time out returning its receipt.
                    # Never replay the mutation. Recover only one target that is both newly created
                    # and still at the exact requested network URL; redirects and ambiguity fail closed.
                    candidates = []
                    # Chromium can expose the new target just after the timed-out /json/new response.
                    # Poll target discovery only; never replay target creation.
                    for attempt in range(10):
                        candidates = [
                            item for item in self.cdp.tabs()
                            if str(item.get("id")) not in before_ids
                            and item.get("url") == network_url
                            and item.get("type", "page") == "page"
                        ]
                        if candidates or attempt == 9:
                            break
                        time.sleep(0.1)
                    if len(candidates) != 1:
                        raise
                    tab = candidates[0]
                    recovered_unknown_outcome = True
                data = {"tabId": tab.get("id", ""), "url": output_url}
                try:
                    ws_url = tab.get("webSocketDebuggerUrl")
                    if ws_url:
                        with self.cdp.session(ws_url) as new_session:
                            target = self._screen_target(new_session)
                            if target:
                                data["screenTarget"] = target
                except Exception:
                    pass  # presentation is best-effort; never fail a committed navigation
                if recovered_unknown_outcome:
                    data["recoveredUnknownOutcome"] = True
                return action_result(accepted=True, action_id=action_id, state=state,
                    actuator="browser.cdp", summary=f"opened {output_url}",
                    data=data,
                    evidence_items=[evidence("browser_navigation", tabId=tab.get("id", ""), url=output_url)])
        except LeaseRejected as exc:
            return action_result(accepted=False, action_id=action_id, state=exc.state,
                                 actuator="browser.cdp", summary="navigation rejected", reason=exc.reason)

    def action(self, request):
        action = str(request.get("browser_action", ""))
        if action not in {"navigate", "query", "focus", "click", "fill"}:
            raise ValueError("unsupported browser action")
        tabs = {tab.get("id"): tab for tab in self.cdp.tabs()}
        tab = tabs.get(str(request.get("tab_id", "")))
        if not tab:
            raise ValueError("tab selector did not resolve")
        # Read-only does not mean authority-free: semantic reads remain inside the domain policy.
        task_id = str(request.get("task_id") or "")
        current_url = self._current_url(tab.get("url", ""), task_id)
        output_url = self._output_url(current_url)
        selector = str(request.get("selector", ""))
        if len(selector) > 2048:
            raise ValueError("selector too long")
        if action == "navigate":
            target_url = self._url(request.get("url", ""), task_id)
            target_network_url = self._network_url(target_url, task_id)
            target_output_url = self._output_url(target_url)
        elif action == "query":
            if not selector:
                raise ValueError("selector required")
            value = self._query_with_readiness_retry(tab, selector, task_id)
            provenance = {"source": "web_page", "trust": "untrusted", "url": output_url}
            value["provenance"] = provenance
            state = self.control.snapshot()
            return {
                "accepted": True, "actionId": str(request.get("action_id", "")),
                "humanEpoch": state["humanEpoch"],
                "startingWorldRevision": state["worldRevision"],
                "endingWorldRevision": state["worldRevision"],
                "actuator": "browser.cdp.read_only", "presentationMethod": "semantic",
                "summary": "untrusted browser content observed",
                "data": {"tabId": tab["id"], "url": output_url, "result": value,
                         "provenance": provenance},
                "evidence": [evidence("browser_observation", tabId=tab["id"], url=output_url,
                                      trust="untrusted")],
                "retrySafety": "safe", "warnings": ["untrusted_web_content"],
            }
        else:
            if not selector:
                raise ValueError("selector required")
        action_id, epoch, revision, _ = require_action_envelope(request)
        try:
            with self.cdp.session(tab["webSocketDebuggerUrl"]) as session:
                node_id = None
                if action in {"focus", "click", "fill"}:
                    node_id, _node = self._resolve_node(session, selector)
                with self.control.commit(epoch, revision) as state:
                    if action == "navigate":
                        value = session.command("Page.navigate", {"url": target_network_url})
                        value = dict(value or {})
                        value["screenTarget"] = self._screen_target(session)
                        result_url = target_output_url
                    elif action == "click":
                        value = self._click_node(session, int(node_id))
                        value["screenTarget"] = self._screen_target(
                            session, value.get("x"), value.get("y"))
                        result_url = output_url
                    elif action == "focus":
                        value = self._focus_node(session, int(node_id))
                        result_url = output_url
                    else:
                        value = self._fill_node(
                            session, int(node_id), str(request.get("value", ""))[:65536]
                        )
                        result_url = output_url
                    # Build the response while the commit context still holds
                    # the pre-mutation revision; action_result reports the
                    # resulting revision as +1. Returning after __exit__ would
                    # double-increment the revision advertised to callers.
                    return action_result(accepted=True, action_id=action_id, state=state,
                        actuator="browser.cdp", summary=f"browser {action} committed",
                        data={"tabId": tab["id"], "url": result_url, "result": value,
                              **({"screenTarget": value["screenTarget"]}
                                 if isinstance(value, dict) and value.get("screenTarget") else {})},
                        evidence_items=[evidence("browser_effect", tabId=tab["id"], action=action,
                                                 resultObserved=value is not None)])
        except LeaseRejected as exc:
            return action_result(accepted=False, action_id=action_id, state=exc.state,
                                 actuator="browser.cdp", summary="browser action rejected", reason=exc.reason)
