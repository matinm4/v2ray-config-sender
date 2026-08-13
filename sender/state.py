"""Queue persistence: load / save ``state/queue.json`` atomically."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .planner import Queue

LOG = logging.getLogger(__name__)


def load_queue(path: Path) -> Queue | None:
    """Read the queue, or return None when absent/unreadable/outdated."""
    path = Path(path)
    if not path.is_file():
        LOG.info("no queue at %s", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("queue %s unreadable (%s) — will rebuild", path, exc)
        return None
    queue = Queue.from_dict(data)
    if queue is None:
        LOG.warning("queue %s has an incompatible version — will rebuild", path)
    return queue


def save_queue(path: Path, queue: Queue) -> None:
    """Write the queue atomically so a killed run cannot corrupt it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(queue.to_dict(), ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(tmp, path)
    LOG.info("queue saved: %d pending batches → %s", queue.pending_count, path)
