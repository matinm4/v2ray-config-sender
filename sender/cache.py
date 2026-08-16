"""Persistent dedupe cache.

The cache is a JSON file committed back to the repository by the workflow, so it
survives across runs. A config that has ever been sent — to *any* channel — is
never sent again while its entry is alive. Entries expire after
``cache.ttl_days`` (0 = never), and the file is trimmed to ``cache.max_entries``
oldest-first when it grows past the limit.

Writes are atomic (temp file + replace) so an interrupted run cannot leave a
truncated cache behind.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class Cache:
    """Fingerprint → metadata store with TTL and size trimming."""

    def __init__(self, path: Path, ttl_days: int = 30, max_entries: int = 200_000) -> None:
        self.path = Path(path)
        self.ttl_seconds = max(0, int(ttl_days)) * 86_400
        self.max_entries = max(0, int(max_entries))
        self.entries: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._load()

    # -- persistence -----------------------------------------------------------
    def _load(self) -> None:
        if not self.path.is_file():
            LOG.info("cache %s does not exist yet — starting empty", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("cache %s is unreadable (%s) — starting empty", self.path, exc)
            return

        if isinstance(data, dict) and "entries" in data:
            raw = data.get("entries") or {}
        elif isinstance(data, dict):
            raw = data  # legacy flat mapping
        elif isinstance(data, list):
            raw = {str(item): {} for item in data}  # legacy list of fingerprints
        else:
            LOG.warning("cache %s has an unexpected shape — starting empty", self.path)
            return

        now = time.time()
        expired = 0
        for key, meta in raw.items():
            if not isinstance(meta, dict):
                meta = {}
            first_seen = float(meta.get("ts") or meta.get("first_seen") or now)
            if self.ttl_seconds and now - first_seen > self.ttl_seconds:
                expired += 1
                continue
            self.entries[str(key)] = {
                "ts": first_seen,
                "last": float(meta.get("last") or first_seen),
                "channel": meta.get("channel", ""),
                "hits": int(meta.get("hits", 1)),
            }
        if expired:
            self._dirty = True
        LOG.info("cache loaded: %d live entries, %d expired", len(self.entries), expired)

    def _trim(self) -> None:
        if not self.max_entries or len(self.entries) <= self.max_entries:
            return
        ordered = sorted(self.entries.items(), key=lambda kv: kv[1].get("ts", 0.0))
        drop = len(self.entries) - self.max_entries
        for key, _ in ordered[:drop]:
            self.entries.pop(key, None)
        LOG.info("cache trimmed by %d entries (limit %d)", drop, self.max_entries)
        self._dirty = True

    def save(self, force: bool = False) -> bool:
        """Write the cache atomically. Returns True when a write happened."""
        if not self._dirty and not force:
            return False
        self._trim()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": int(time.time()),
            "count": len(self.entries),
            "entries": self.entries,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        os.replace(tmp, self.path)
        self._dirty = False
        LOG.info("cache saved: %d entries → %s", len(self.entries), self.path)
        return True

    # -- queries ---------------------------------------------------------------
    def __contains__(self, key: str) -> bool:
        return key in self.entries

    def __len__(self) -> int:
        return len(self.entries)

    def has(self, key: str) -> bool:
        return key in self.entries

    def add(self, key: str, channel: str = "") -> None:
        """Record *key* as sent. ``ts`` keeps the first sighting, ``last`` the latest."""
        now = time.time()
        existing = self.entries.get(key)
        if existing:
            existing["hits"] = int(existing.get("hits", 1)) + 1
            existing["last"] = now
            existing["channel"] = channel or existing.get("channel", "")
        else:
            self.entries[key] = {"ts": now, "last": now, "channel": channel, "hits": 1}
        self._dirty = True

    def add_many(self, keys: list[str], channel: str = "") -> None:
        for key in keys:
            self.add(key, channel)

    def last_sent(self, key: str) -> float:
        """When *key* last went out (0.0 if never). Used to order recycled configs."""
        meta = self.entries.get(key)
        if not meta:
            return 0.0
        return float(meta.get("last") or meta.get("ts") or 0.0)

    def times_sent(self, key: str) -> int:
        meta = self.entries.get(key)
        return int(meta.get("hits", 1)) if meta else 0
