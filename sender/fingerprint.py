"""Semantic fingerprints for deduplication.

Two configs are "the same" when they point at the same server with the same
credentials and transport, regardless of remark text, parameter order, ad
pollution, or base64 padding. The fingerprint is a SHA-256 over a canonical
tuple, so the cache survives cosmetic rewrites of the source list.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qsl, unquote

from .cleaner import _SCHEME_RE, _b64_decode, _normalize_ss

# Parameters that identify the connection. Everything else (fp, remark, ad junk,
# allowInsecure, …) is cosmetic and deliberately excluded.
_IDENTITY_PARAMS = (
    "type",
    "security",
    "encryption",
    "flow",
    "headertype",
    "path",
    "servicename",
    "sni",
    "host",
    "pbk",
    "sid",
    "mode",
    "alpn",
    "obfs",
    "password",
    "auth",
    "method",
)


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", unquote(str(value or ""))).strip("/").lower()


def _digest(parts: object) -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:40]


def _vmess_fingerprint(body: str) -> str | None:
    decoded = _b64_decode(body.split("#", 1)[0])
    if decoded is None:
        return None
    try:
        data = json.loads(decoded.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    identity = {
        "scheme": "vmess",
        "add": _norm(data.get("add", "")),
        "port": str(data.get("port", "")).strip(),
        "id": _norm(data.get("id", "")),
        "net": _norm(data.get("net", "")),
        "tls": _norm(data.get("tls", "")),
        "path": _norm(data.get("path", "")),
        "sni": _norm(data.get("sni", "")),
    }
    if not identity["add"] or not identity["port"]:
        return None
    return _digest(identity)


def fingerprint(uri: str) -> str | None:
    """Return a stable fingerprint for *uri*, or None when it cannot be parsed."""
    match = _SCHEME_RE.match((uri or "").strip())
    if not match:
        return None
    scheme = match.group("scheme").lower()
    body = match.group("body")

    if scheme == "vmess":
        return _vmess_fingerprint(body)

    if scheme == "ss":
        body = _normalize_ss(body)

    head = body.partition("#")[0]
    before_query, _, query = head.partition("?")

    userinfo, _, hostpart = before_query.rpartition("@")
    hostport = hostpart.split("/", 1)[0]
    if not hostport:
        return None

    host, _, port = hostport.rpartition(":")
    if not host:  # no port present
        host, port = hostport, ""

    params = dict(parse_qsl(query, keep_blank_values=True, strict_parsing=False))
    # Query params are namespaced so a "host=" parameter cannot shadow the
    # server address — that collision would make different servers look equal.
    identity = {
        "scheme": scheme,
        "user": _norm(userinfo),
        "server": _norm(host.strip("[]")),
        "port": port.strip(),
        "params": {name: _norm(params.get(name, "")) for name in _IDENTITY_PARAMS},
    }
    return _digest(identity)


def endpoint_key(uri: str) -> str | None:
    """A coarser key: same server:port, ignoring credentials and transport.

    Useful for capping how many configs from one host land in a single batch.
    """
    match = _SCHEME_RE.match((uri or "").strip())
    if not match:
        return None
    body = match.group("body")
    if match.group("scheme").lower() == "vmess":
        decoded = _b64_decode(body.split("#", 1)[0])
        if decoded is None:
            return None
        try:
            data = json.loads(decoded.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            return None
        return f"{_norm(data.get('add', ''))}:{str(data.get('port', '')).strip()}"

    head = body.partition("#")[0].partition("?")[0]
    hostport = head.rpartition("@")[2].split("/", 1)[0]
    return _norm(hostport) or None
