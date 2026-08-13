"""Queue building and time slotting.

The planner turns the raw source list into a *queue*: for every channel, a list
of batches, each stamped with the UTC time it becomes due. A cron-driven run then
sends only the batches whose ``due_at`` has passed.

Slot times are spread evenly across ``schedule.window_hours``. With a 24-hour
window, a 100-config quota and a batch size of 10, a channel gets 10 batches
roughly 2h24m apart.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .cache import Cache
from .cleaner import clean
from .fingerprint import endpoint_key, fingerprint
from .settings import Channel, Settings

LOG = logging.getLogger(__name__)

QUEUE_VERSION = 2


@dataclass
class Batch:
    """One outgoing message: a group of configs due at a specific time."""

    index: int
    due_at: float
    configs: list[str] = field(default_factory=list)
    fingerprints: list[str] = field(default_factory=list)
    sent_at: float | None = None
    message_id: int | None = None

    @property
    def sent(self) -> bool:
        return self.sent_at is not None

    def is_due(self, now: float, grace_seconds: float = 0.0) -> bool:
        return not self.sent and now >= self.due_at - grace_seconds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Batch":
        return cls(
            index=int(data.get("index", 0)),
            due_at=float(data.get("due_at", 0.0)),
            configs=list(data.get("configs") or []),
            fingerprints=list(data.get("fingerprints") or []),
            sent_at=data.get("sent_at"),
            message_id=data.get("message_id"),
        )


@dataclass
class ChannelPlan:
    """Everything scheduled for one channel in the current window."""

    username: str
    key: str
    batches: list[Batch] = field(default_factory=list)

    @property
    def total_configs(self) -> int:
        return sum(len(b.configs) for b in self.batches)

    @property
    def pending(self) -> list[Batch]:
        return [b for b in self.batches if not b.sent]

    def due(self, now: float, grace_seconds: float = 0.0) -> list[Batch]:
        return [b for b in self.batches if b.is_due(now, grace_seconds)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "key": self.key,
            "batches": [b.to_dict() for b in self.batches],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelPlan":
        return cls(
            username=str(data.get("username", "")),
            key=str(data.get("key", "")),
            batches=[Batch.from_dict(b) for b in data.get("batches") or []],
        )


@dataclass
class Queue:
    """A full window's plan, persisted to ``state/queue.json``."""

    window_start: float
    window_end: float
    created_at: float
    channels: list[ChannelPlan] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def total_configs(self) -> int:
        return sum(c.total_configs for c in self.channels)

    @property
    def pending_count(self) -> int:
        return sum(len(c.pending) for c in self.channels)

    def exhausted(self, now: float) -> bool:
        """True when nothing is left to send (or the window has fully elapsed)."""
        return self.pending_count == 0

    def expired(self, now: float) -> bool:
        return now >= self.window_end

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": QUEUE_VERSION,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "created_at": self.created_at,
            "stats": self.stats,
            "channels": [c.to_dict() for c in self.channels],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Queue | None":
        if not isinstance(data, dict) or int(data.get("version", 0)) != QUEUE_VERSION:
            return None
        try:
            return cls(
                window_start=float(data["window_start"]),
                window_end=float(data["window_end"]),
                created_at=float(data.get("created_at", data["window_start"])),
                channels=[ChannelPlan.from_dict(c) for c in data.get("channels") or []],
                stats=dict(data.get("stats") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            LOG.warning("queue payload is malformed (%s) — will rebuild", exc)
            return None


# --------------------------------------------------------------------------- #
# source loading and cleaning
# --------------------------------------------------------------------------- #
@dataclass
class Prepared:
    """A cleaned, deduplicated config ready to be scheduled."""

    uri: str
    fp: str
    endpoint: str


def load_source(settings: Settings) -> list[str]:
    """Read the raw source file, one config per line."""
    path = settings.source_path
    if not path.is_file():
        raise SystemExit(f"source file not found: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln and not ln.startswith(("#", "//"))]


def prepare(
    raw_lines: Iterable[str],
    settings: Settings,
    cache: Cache,
) -> tuple[list[Prepared], dict[str, int]]:
    """Clean every line, drop invalid/cached/duplicate entries.

    Cleaning happens with a neutral placeholder remark so the fingerprint is
    channel-independent; the real channel name is stamped in at send time.
    """
    stats = {"input": 0, "invalid": 0, "oversized": 0, "cached": 0, "dup_in_batch": 0, "ready": 0}
    cfg = dict(settings.cleaning)
    # A single config longer than a whole message can never be delivered.
    size_limit = max(256, int(settings.message.get("max_chars", 4000)) - 200)
    seen: set[str] = set()
    prepared: list[Prepared] = []

    for line in raw_lines:
        stats["input"] += 1
        result = clean(line, "__PLACEHOLDER__", cfg)
        if not result.ok or result.uri is None:
            stats["invalid"] += 1
            LOG.debug("dropped (%s): %s", result.reason, line[:90])
            continue

        if len(result.uri) > size_limit:
            stats["oversized"] += 1
            LOG.debug("dropped (%d chars > %d): %s", len(result.uri), size_limit, result.uri[:90])
            continue

        fp = fingerprint(result.uri)
        if fp is None:
            stats["invalid"] += 1
            continue
        if fp in seen:
            stats["dup_in_batch"] += 1
            continue
        if cache.has(fp):
            stats["cached"] += 1
            continue

        seen.add(fp)
        prepared.append(Prepared(uri=result.uri, fp=fp, endpoint=endpoint_key(result.uri) or ""))

    stats["ready"] = len(prepared)
    return prepared, stats


# --------------------------------------------------------------------------- #
# distribution across channels
# --------------------------------------------------------------------------- #
def _interleave_by_endpoint(items: list[Prepared], rng: random.Random) -> list[Prepared]:
    """Reorder so configs sharing a server are spread out instead of clustered."""
    buckets: dict[str, list[Prepared]] = {}
    for item in items:
        buckets.setdefault(item.endpoint, []).append(item)
    for bucket in buckets.values():
        rng.shuffle(bucket)

    ordered: list[Prepared] = []
    groups = sorted(buckets.values(), key=len, reverse=True)
    while any(groups):
        for group in groups:
            if group:
                ordered.append(group.pop(0))
    return ordered


def distribute(
    items: list[Prepared], channels: list[Channel], settings: Settings
) -> dict[str, list[Prepared]]:
    """Split *items* between channels, honouring each channel's daily quota.

    ``round_robin`` deals one config to each channel in turn — every channel gets
    a comparable mix of protocols and servers. ``chunk`` gives each channel one
    contiguous slice instead.
    """
    dist = settings.distribution
    rng = random.Random(dist.get("seed"))

    pool = list(items)
    if dist.get("shuffle", True):
        pool = _interleave_by_endpoint(pool, rng)

    quotas = {c.key: int(c.daily_quota) for c in channels}
    result: dict[str, list[Prepared]] = {c.key: [] for c in channels}

    if str(dist.get("mode", "round_robin")).lower() == "chunk":
        cursor = 0
        for channel in channels:
            take = min(quotas[channel.key], max(0, len(pool) - cursor))
            result[channel.key] = pool[cursor : cursor + take]
            cursor += take
        return result

    # round-robin
    cursor = 0
    open_channels = [c for c in channels]
    while cursor < len(pool) and open_channels:
        progressed = False
        for channel in list(open_channels):
            if cursor >= len(pool):
                break
            if len(result[channel.key]) >= quotas[channel.key]:
                open_channels.remove(channel)
                continue
            result[channel.key].append(pool[cursor])
            cursor += 1
            progressed = True
        if not progressed:
            break
    return result


# --------------------------------------------------------------------------- #
# time slotting
# --------------------------------------------------------------------------- #
def _slot_times(count: int, start: float, window_seconds: float) -> list[float]:
    """Evenly spaced due-times; the first is immediate, the last inside the window."""
    if count <= 0:
        return []
    if count == 1:
        return [start]
    step = window_seconds / count
    return [start + step * i for i in range(count)]


def build_queue(settings: Settings, cache: Cache, now: float | None = None) -> Queue:
    """Create a fresh queue for the next window."""
    now = time.time() if now is None else now
    sched = settings.schedule
    window_seconds = float(sched["window_hours"]) * 3600.0
    batch_size = int(sched["batch_size"])

    raw = load_source(settings)
    prepared, stats = prepare(raw, settings, cache)
    LOG.info(
        "source: %d lines → %d ready (invalid=%d, oversized=%d, already sent=%d, dup=%d)",
        stats["input"], stats["ready"], stats["invalid"], stats["oversized"],
        stats["cached"], stats["dup_in_batch"],
    )

    per_channel = distribute(prepared, settings.channels, settings)

    plans: list[ChannelPlan] = []
    for channel in settings.channels:
        items = per_channel.get(channel.key, [])
        groups = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]
        times = _slot_times(len(groups), now, window_seconds)
        batches = [
            Batch(
                index=i + 1,
                due_at=times[i],
                configs=[p.uri for p in group],
                fingerprints=[p.fp for p in group],
            )
            for i, group in enumerate(groups)
        ]
        plans.append(ChannelPlan(username=channel.username, key=channel.key, batches=batches))
        LOG.info(
            "%s: %d configs in %d batches, every %s",
            channel.username,
            len(items),
            len(batches),
            _pretty_step(window_seconds, len(batches)),
        )

    queue = Queue(
        window_start=now,
        window_end=now + window_seconds,
        created_at=now,
        channels=plans,
        stats=stats,
    )
    if queue.total_configs == 0:
        LOG.warning(
            "nothing to schedule — every config in %s is either invalid or already sent",
            settings.source_path.name,
        )
    return queue


def _pretty_step(window_seconds: float, batches: int) -> str:
    if batches <= 0:
        return "n/a"
    delta = timedelta(seconds=window_seconds / batches)
    total_minutes = int(delta.total_seconds() // 60)
    return f"{total_minutes // 60}h{total_minutes % 60:02d}m"


def describe(queue: Queue, now: float | None = None) -> str:
    """Human-readable summary used in logs and the workflow summary."""
    now = time.time() if now is None else now
    lines = [
        f"window: {_ts(queue.window_start)} → {_ts(queue.window_end)}",
        f"total configs: {queue.total_configs}, pending batches: {queue.pending_count}",
    ]
    for plan in queue.channels:
        pending = plan.pending
        next_due = min((b.due_at for b in pending), default=None)
        lines.append(
            f"  {plan.username}: {plan.total_configs} configs, "
            f"{len(plan.batches) - len(pending)}/{len(plan.batches)} batches sent"
            + (f", next at {_ts(next_due)}" if next_due else ", complete")
        )
    return "\n".join(lines)


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
