"""Ad / username stripping and remark rewriting for V2Ray-family config URIs.

Two modes, selected with ``cleaning.aggressive``:

* **smart** (default) — the remark (the ``#...`` part) is always replaced with the
  destination channel. Ad-only query parameters are dropped. Structural params
  (``host``, ``sni``, ``path``, ``serviceName`` …) are only touched when the
  advertisement can be removed without destroying a working value: a value that
  is a valid hostname is always kept as-is, a value that is *only* an ad is
  dropped, and a value that mixes both has just the ad part cut out.
* **aggressive** — every ``@username`` / telegram-ish token is removed wherever
  it appears, even inside otherwise-functional values. Zero ads, some risk of
  breaking a config whose real path happens to be the ad text.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, unquote

LOG = logging.getLogger(__name__)

# Protocols we understand well enough to rewrite safely.
URI_SCHEMES = (
    "vless",
    "vmess",
    "trojan",
    "ss",
    "ssr",
    "hysteria",
    "hysteria2",
    "hy2",
    "tuic",
    "socks",
    "wireguard",
    "warp",
    "anytls",
    "mieru",
)

_SCHEME_RE = re.compile(r"^(?P<scheme>[a-z0-9+.\-]+)://(?P<body>.*)$", re.IGNORECASE | re.DOTALL)

# A mention such as "@Farah_VPN". Telegram usernames are 5-32 chars, but ad
# injectors are sloppy, so accept 3+.
_MENTION_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{2,}")

# Words that only ever show up in promotional text.
_AD_WORDS = (
    "telegram",
    "t\\.me",
    "tg://",
    "join",
    "subscribe",
    "subscribtion",
    "channel",
    "kanal",
    "پروکسی",
    "کانال",
    "جوین",
    "تلگرام",
    "عضو",
)
_AD_WORD_RE = re.compile("|".join(_AD_WORDS), re.IGNORECASE)

# "@x---@x---@x" or "v2rayNplus--v2rayNplus--v2rayNplus": the same chunk repeated.
# The overall match must also be reasonably long (see _find_repeat) so that a
# short, legitimate pattern is not mistaken for spam.
_REPEAT_RE = re.compile(r"(?P<tok>[A-Za-z0-9_@.\-]{2,}?)(?:[\s,_\-]{1,4}(?P=tok)){2,}")

# A single repetition only counts as spam when the repeated token is long,
# e.g. "MARAMBASHI_MARAMBASHI".
_REPEAT_PAIR_RE = re.compile(r"(?P<tok>[A-Za-z0-9_@.\-]{6,}?)[\s,_\-]{1,4}(?P=tok)")

# Hostname: at least two dot-separated labels, ASCII, sane TLD.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)+[A-Za-z][A-Za-z0-9-]{1,62}$"
)
_HOSTNAME_SEARCH_RE = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]{1,62}"
)

# Early-data hint that some servers require; worth rescuing from a polluted path.
_EARLY_DATA_RE = re.compile(r"\bed=\d+")

# Real Xray / sing-box URI parameters. Anything outside this set is suspicious.
KNOWN_PARAMS = {
    # transport / security
    "type", "encryption", "security", "flow", "headertype", "host", "path", "sni",
    "alpn", "fp", "pbk", "sid", "spx", "mode", "servicename", "authority", "seed",
    "quicsecurity", "key", "packetencoding", "extra", "allowinsecure", "insecure",
    "skip-cert-verify", "xhttpmode", "heartbeatperiod", "scmaxeachpostbytes",
    "scmaxconcurrentposts", "scminpostsintervalms", "downloadsettings",
    # protocol-specific
    "obfs", "obfs-password", "obfs-host", "obfs-uri", "upmbps", "downmbps", "up",
    "down", "auth", "auth-str", "password", "peer", "protocol", "plugin",
    "congestion_control", "congestioncontrol", "udp_relay_mode", "udprelaymode",
    "alterid", "aid", "disable_sni", "reduce_rtt", "publickey", "privatekey",
    "address", "reserved", "mtu", "workers", "encryption_method", "method",
    "udp", "tfo", "mport", "hpkp", "pinsha256", "ech", "ecc", "eci",
}

# Parameter values that are functional and must not be mangled blindly.
_STRUCTURAL_HOSTLIKE = {"host", "sni", "peer", "authority", "obfs-host", "address"}
_STRUCTURAL_PATHLIKE = {"path", "servicename", "obfs-uri", "spx", "seed"}

_VMESS_REMARK_KEYS = ("ps", "remark", "remarks", "name")

# Characters left un-escaped when rebuilding a query string. Clients accept these
# literally and re-encoding them breaks some servers — notably "?" and "=" inside
# a ``path`` value, where "/?ed=2560" is a real early-data hint. "&" and "#" are
# deliberately excluded: leaving those raw would change the URI's structure.
_VALUE_SAFE = "/:[]@?=,+$!*'()~"
_NAME_SAFE = "-_."

# Remark characters that stay literal so the channel name is readable in clients.
_REMARK_SAFE = "@+:/_-.!~*'()"


@dataclass
class CleanResult:
    """Outcome of cleaning one line."""

    uri: str | None
    changed: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.uri)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _b64_decode(data: str) -> bytes | None:
    """Decode standard or URL-safe base64, tolerating missing padding."""
    cleaned = re.sub(r"\s+", "", data)
    for variant in (cleaned, cleaned.replace("-", "+").replace("_", "/")):
        padded = variant + "=" * (-len(variant) % 4)
        try:
            return base64.b64decode(padded, validate=False)
        except (binascii.Error, ValueError):
            continue
    return None


def _b64_encode(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def is_valid_host(value: str) -> bool:
    """True when *value* is a usable hostname or IP literal."""
    candidate = value.strip().strip(".")
    if not candidate:
        return False
    try:
        ipaddress.ip_address(candidate.strip("[]"))
        return True
    except ValueError:
        pass
    return bool(_HOSTNAME_RE.match(candidate))


def looks_like_ad(value: str) -> bool:
    """Heuristic: does *value* carry promotional text?"""
    if not value:
        return False
    decoded = unquote(value)
    return bool(
        _MENTION_RE.search(decoded)
        or _AD_WORD_RE.search(decoded)
        or _find_repeat(decoded)
    )


def _find_repeat(text: str) -> re.Match[str] | None:
    """Find a spam-style repeated token (``@x---@x---@x``), ignoring short noise."""
    for match in _REPEAT_RE.finditer(text):
        if len(match.group(0)) >= 8:
            return match
    return _REPEAT_PAIR_RE.search(text)


def _drop_repeats(text: str) -> str:
    out = text
    while True:
        match = _find_repeat(out)
        if match is None:
            return out
        out = out[: match.start()] + out[match.end() :]


def strip_ads(text: str) -> str:
    """Remove mention/ad tokens from *text*, then tidy the leftover punctuation."""
    out = _drop_repeats(text)
    out = _MENTION_RE.sub("", out)
    # Drop ad words together with any adjacent glue characters.
    out = re.sub(r"[\s_\-+.,:]*(?:%s)[\s_\-+.,:]*" % "|".join(_AD_WORDS), "", out, flags=re.IGNORECASE)
    out = re.sub(r"\?{2,}", "?", out)
    out = re.sub(r"/{2,}", "/", out)
    out = re.sub(r"[\s\-_+,]{2,}", "", out)
    return out.strip(" \t-_+,:;")


def _is_pure_ad(text: str) -> bool:
    """True when *text* contains nothing but promotional tokens."""
    return looks_like_ad(text) and not strip_ads(text).strip("/?=&_- \t")


def _salvage_host(value: str) -> str | None:
    """Pull a genuine hostname out of a polluted ``host=`` / ``sni=`` value."""
    decoded = unquote(value)
    candidates: list[str] = []
    for chunk in _HOSTNAME_SEARCH_RE.findall(decoded):
        # Ad injectors glue their handle to the real host with "---" or "__".
        for piece in re.split(r"[-_]{2,}|[\s,;]+", chunk):
            labels = piece.strip(".").split(".")
            for start in range(len(labels) - 1):
                candidates.append(".".join(labels[start:]))
    for candidate in sorted(set(candidates), key=len, reverse=True):
        if is_valid_host(candidate) and not looks_like_ad(candidate):
            return candidate
    return None


def _clean_pathlike(value: str, aggressive: bool) -> str | None:
    """Clean a path-shaped value. Returns None when the param should be dropped."""
    decoded = unquote(value)
    if not looks_like_ad(decoded):
        return value

    fallback = "/" if decoded.startswith("/") else None
    core = decoded.strip("/ \t")
    # A path with several segments or query-ish syntax has real structure worth
    # keeping; a single segment is usually the server's own route.
    structural = "/" in core or "?" in core or "=" in core

    if not structural:
        if aggressive or _is_pure_ad(core):
            return fallback if _is_pure_ad(core) else None
        return value  # ad mixed into the only route we have — keep it working

    stripped = strip_ads(decoded)
    early = _EARLY_DATA_RE.search(decoded)
    if early and early.group(0) not in stripped:
        stripped = f"{stripped}{'&' if '?' in stripped else '?'}{early.group(0)}"
    return stripped or fallback


# --------------------------------------------------------------------------- #
# query-string cleaning (vless / trojan / ss / hysteria / tuic …)
# --------------------------------------------------------------------------- #
def _clean_query(query: str, aggressive: bool, cfg: dict[str, Any]) -> tuple[str, bool]:
    if not query:
        return "", False

    extra_names = {str(n).lower() for n in cfg.get("extra_ad_param_names", [])}
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=False)
    if not pairs:  # unparsable junk — leave it alone rather than corrupt it
        return query, False

    out: list[tuple[str, str]] = []
    changed = False

    for name, value in pairs:
        lname = name.strip().lower()

        # 1. Parameter names that are themselves advertisements.
        if lname in extra_names or (lname not in KNOWN_PARAMS and looks_like_ad(name)):
            changed = True
            continue

        # 2. Unknown, non-ad names with garbage characters (e.g. "f`p`").
        if lname not in KNOWN_PARAMS and re.search(r"[^a-z0-9_.\-]", lname):
            changed = True
            continue

        # 3. Truncated / malformed JSON blobs.
        if lname == "extra" and value:
            try:
                json.loads(unquote(value))
            except (json.JSONDecodeError, ValueError):
                changed = True
                continue

        if not cfg.get("strip_ad_params", True) or not value:
            out.append((name, value))
            continue

        # 4. Host-like values: keep if already valid, else salvage or drop.
        if lname in _STRUCTURAL_HOSTLIKE:
            decoded = unquote(value)
            if is_valid_host(decoded):
                out.append((name, decoded))
                continue
            if not looks_like_ad(decoded) and not aggressive:
                out.append((name, value))
                continue
            salvaged = _salvage_host(decoded)
            changed = True
            if salvaged:
                out.append((name, salvaged))
            continue

        # 5. Path-like values: cut the ad, keep the route.
        if lname in _STRUCTURAL_PATHLIKE:
            cleaned = _clean_pathlike(value, aggressive)
            if cleaned is None:
                changed = True
                continue
            if cleaned != value:
                changed = True
            out.append((name, cleaned))
            continue

        # 6. Everything else: only intervene when an ad is clearly embedded.
        if looks_like_ad(value):
            cleaned = strip_ads(unquote(value))
            changed = True
            if cleaned:
                out.append((name, cleaned))
            continue

        out.append((name, value))

    return _build_query(out), changed


def _build_query(pairs: list[tuple[str, str]]) -> str:
    """Re-assemble a query string, keeping client-significant characters literal.

    Empty values keep their ``=`` (``sid=``) because that is how clients emit
    them and dropping it changes the parameter's shape.
    """
    return "&".join(
        f"{quote(str(name), safe=_NAME_SAFE)}={quote(str(value), safe=_VALUE_SAFE)}"
        for name, value in pairs
    )


# --------------------------------------------------------------------------- #
# vmess (base64-encoded JSON)
# --------------------------------------------------------------------------- #
def _clean_vmess(body: str, remark: str, aggressive: bool, cfg: dict[str, Any]) -> CleanResult:
    payload = body.split("#", 1)[0]
    decoded = _b64_decode(payload)
    if decoded is None:
        return CleanResult(None, reason="vmess: base64 decode failed")
    try:
        data = json.loads(decoded.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return CleanResult(None, reason="vmess: payload is not JSON")
    if not isinstance(data, dict):
        return CleanResult(None, reason="vmess: payload is not an object")
    if not str(data.get("add", "")).strip() or not str(data.get("port", "")).strip():
        return CleanResult(None, reason="vmess: missing address or port")

    changed = False
    if cfg.get("replace_remark", True):
        for key in _VMESS_REMARK_KEYS:
            if key in data and str(data[key]) != remark:
                changed = True
        for key in _VMESS_REMARK_KEYS:
            data.pop(key, None)
        data["ps"] = remark
        changed = True

    if cfg.get("strip_ad_params", True):
        for key in ("host", "sni"):
            value = str(data.get(key, "") or "")
            if not value:
                continue
            if is_valid_host(value):
                continue
            if not looks_like_ad(value) and not aggressive:
                continue
            salvaged = _salvage_host(value)
            data[key] = salvaged or ""
            changed = True

        for key in ("path", "servicename", "serviceName"):
            value = data.get(key)
            if not isinstance(value, str) or not value:
                continue
            cleaned = _clean_pathlike(value, aggressive)
            cleaned = "" if cleaned is None else cleaned
            if cleaned != value:
                data[key] = cleaned
                changed = True

        for key in list(data.keys()):
            if key in _VMESS_REMARK_KEYS:
                continue
            if looks_like_ad(key):
                data.pop(key, None)
                changed = True

    encoded = _b64_encode(json.dumps(data, ensure_ascii=False, separators=(", ", ": ")).encode("utf-8"))
    return CleanResult(f"vmess://{encoded}", changed=changed)


# --------------------------------------------------------------------------- #
# ss:// (may be fully base64 or "method:pass@host:port")
# --------------------------------------------------------------------------- #
def _normalize_ss(body: str) -> str:
    """Return the ss body with a plain ``userinfo@host:port`` shape when possible.

    The fragment is examined separately: an ad remark like ``#@Channel`` contains
    an ``@`` of its own and would otherwise make a fully-encoded body look plain.
    """
    head, sep, fragment = body.partition("#")
    if "@" not in head:
        decoded = _b64_decode(head)
        if decoded is not None:
            text = decoded.decode("utf-8", errors="replace")
            if "@" in text:
                head = text
    return head + sep + fragment


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def clean(raw: str, channel: str, cfg: dict[str, Any] | None = None) -> CleanResult:
    """Clean one config line and stamp *channel* as its remark."""
    cfg = cfg or {}
    aggressive = bool(cfg.get("aggressive", False))
    template = str(cfg.get("remark_template") or "{channel}")
    remark = template.format(channel=channel)

    line = raw.strip().strip("﻿").replace("‏", "").replace("‎", "")
    if not line or line.startswith(("#", "//")):
        return CleanResult(None, reason="blank or comment")

    match = _SCHEME_RE.match(line)
    if not match:
        return CleanResult(None, reason="no URI scheme")
    scheme = match.group("scheme").lower()
    body = match.group("body")
    if scheme not in URI_SCHEMES:
        return CleanResult(None, reason=f"unsupported scheme {scheme!r}")

    if scheme == "vmess":
        return _clean_vmess(body, remark, aggressive, cfg)

    if scheme == "ss":
        body = _normalize_ss(body)

    # Split off the fragment (remark) and the query string.
    head, _, _fragment = body.partition("#")
    before_query, _, query = head.partition("?")

    if "@" not in before_query and scheme in {"vless", "vmess", "trojan"}:
        return CleanResult(None, reason=f"{scheme}: missing credentials")
    hostport = before_query.rsplit("@", 1)[-1].split("/", 1)[0]
    if not hostport:
        return CleanResult(None, reason="missing host")

    new_query, q_changed = _clean_query(query, aggressive, cfg)

    rebuilt = before_query
    if new_query:
        rebuilt += "?" + new_query
    if cfg.get("replace_remark", True) and remark:
        rebuilt += "#" + quote(remark, safe=_REMARK_SAFE)

    return CleanResult(f"{scheme}://{rebuilt}", changed=q_changed or True)
