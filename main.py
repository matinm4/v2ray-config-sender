#!/usr/bin/env python3
"""Entry point: build a schedule, then send whatever batches are due.

    python main.py run          # normal cron-driven run (default)
    python main.py plan         # (re)build the queue and show it, send nothing
    python main.py status       # print the current queue state
    python main.py preview      # render what the next due messages would look like
    python main.py reset        # clear the queue (keeps the dedupe cache)
    python main.py clear-cache  # forget every sent config (dangerous)

The design assumes the workflow runs on a short cron (every ~15 min). Each run:

1. loads (or rebuilds) ``state/queue.json``,
2. sends every batch whose ``due_at`` has passed,
3. records fingerprints in ``state/cache.json``,
4. commits both files back so the next run knows what happened.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from sender import planner, settings as settings_mod, state, tg
from sender.cache import Cache

LOG = logging.getLogger("main")

ROOT = Path(__file__).resolve().parent


def _setup_logging(level: str = "INFO") -> None:
    # Windows consoles default to cp1252, which cannot encode the arrows and
    # emoji in log lines and rendered messages. Force UTF-8 on both streams.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def _summary(text: str) -> None:
    """Append to the GitHub Actions run summary when available."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")
    except OSError as exc:  # never fail the run over a summary write
        LOG.debug("could not write step summary: %s", exc)


def _load(args: argparse.Namespace) -> tuple[settings_mod.Settings, Cache]:
    cfg = settings_mod.load(ROOT / args.config, root=ROOT)
    if args.dry_run:
        cfg.raw["runtime"]["dry_run"] = True
    cache = Cache(
        cfg.cache_path,
        ttl_days=int(cfg.cache_cfg["ttl_days"]),
        max_entries=int(cfg.cache_cfg["max_entries"]),
    )
    return cfg, cache


def _ensure_queue(
    cfg: settings_mod.Settings, cache: Cache, now: float, force: bool = False
) -> planner.Queue:
    """Return a usable queue, rebuilding when missing, expired, or exhausted."""
    queue = None if force else state.load_queue(cfg.queue_path)

    if queue is not None and queue.expired(now):
        LOG.info("window ended — rebuilding the queue")
        queue = None
    elif queue is not None and queue.exhausted(now):
        if cfg.schedule.get("rebuild_when_exhausted", True):
            LOG.info("every batch in this window was sent — rebuilding early")
            queue = None
        else:
            LOG.info("queue is complete; rebuild_when_exhausted is off, nothing to do")

    if queue is None:
        queue = planner.build_queue(cfg, cache, now=now)
        state.save_queue(cfg.queue_path, queue)
    return queue


async def _run(args: argparse.Namespace) -> int:
    cfg, cache = _load(args)
    now = time.time()

    queue = _ensure_queue(cfg, cache, now, force=args.rebuild)
    grace = float(cfg.schedule.get("grace_minutes", 10)) * 60.0
    max_messages = int(cfg.schedule.get("max_messages_per_run", 6))
    delay = float(cfg.schedule.get("inter_message_delay_seconds", 5))

    due: list[tuple[planner.ChannelPlan, planner.Batch]] = []
    for plan in queue.channels:
        for batch in plan.due(now, grace):
            due.append((plan, batch))
    due.sort(key=lambda pair: pair[1].due_at)

    if not due:
        next_due = min(
            (b.due_at for c in queue.channels for b in c.pending), default=None
        )
        when = planner._ts(next_due) if next_due else "n/a"
        LOG.info("nothing is due yet — next batch at %s", when)
        _summary(f"### Nothing due\nNext batch at **{when}**\n")
        return 0

    if max_messages > 0 and len(due) > max_messages:
        LOG.info("%d batches are due; capping this run at %d", len(due), max_messages)
        due = due[:max_messages]

    creds = None if cfg.dry_run else tg.Credentials.from_env()
    if creds is None:
        creds = tg.Credentials(api_id=0, api_hash="", session="")

    sent_batches = 0
    sent_configs = 0
    failures = 0
    lines: list[str] = []
    # Channel objects carry the per-channel extra_text; the queue only knows names.
    by_key = {c.key: c for c in cfg.channels}

    async with tg.Sender(creds, dry_run=cfg.dry_run) as sender:
        for plan, batch in due:
            total_batches = len(plan.batches)
            # Re-stamp the remark with the real destination channel.
            configs = [
                _restamp(uri, plan.username, cfg.cleaning) for uri in batch.configs
            ]
            channel = by_key.get(plan.key)
            extra = cfg.extra_text_for(channel) if channel else ""
            messages = tg.render(
                configs, plan.username, cfg.message, batch.index, total_batches, extra
            )

            try:
                first_id: int | None = None
                for i, text in enumerate(messages):
                    message_id = await sender.send(plan.username, text)
                    first_id = first_id if first_id is not None else message_id
                    if delay > 0 and i + 1 < len(messages):
                        await asyncio.sleep(delay)
            except Exception as exc:  # noqa: BLE001 — one bad channel must not kill the run
                failures += 1
                LOG.error("batch %d for %s failed: %s", batch.index, plan.username, exc)
                lines.append(f"| {plan.username} | {batch.index}/{total_batches} | ❌ {exc} |")
                continue

            batch.sent_at = time.time()
            batch.message_id = first_id
            if not cfg.dry_run:
                cache.add_many(batch.fingerprints, plan.key)
            sent_batches += 1
            sent_configs += len(configs)
            lines.append(
                f"| {plan.username} | {batch.index}/{total_batches} | ✅ {len(configs)} configs |"
            )

            # Persist after every batch: a crash mid-run never re-sends or loses work.
            if not cfg.dry_run:
                state.save_queue(cfg.queue_path, queue)
                cache.save()

            if delay > 0 and (plan, batch) != due[-1]:
                await asyncio.sleep(delay)

    if not cfg.dry_run:
        state.save_queue(cfg.queue_path, queue)
        cache.save()

    LOG.info(
        "run complete: %d batches / %d configs sent, %d failures, cache holds %d",
        sent_batches, sent_configs, failures, len(cache),
    )
    LOG.info("\n%s", planner.describe(queue))

    _summary(
        "### Send report\n\n"
        "| Channel | Batch | Result |\n|---|---|---|\n"
        + "\n".join(lines)
        + f"\n\n**{sent_configs}** configs in **{sent_batches}** batches • "
        f"cache: **{len(cache)}** fingerprints\n\n```\n{planner.describe(queue)}\n```\n"
    )
    return 1 if failures and sent_batches == 0 else 0


def _restamp(uri: str, channel: str, cleaning_cfg: dict) -> str:
    """Swap the placeholder remark for the destination channel name."""
    from sender.cleaner import clean

    result = clean(uri, channel, cleaning_cfg)
    return result.uri if result.ok and result.uri else uri


def _plan(args: argparse.Namespace) -> int:
    cfg, cache = _load(args)
    queue = planner.build_queue(cfg, cache, now=time.time())
    state.save_queue(cfg.queue_path, queue)
    print(planner.describe(queue))
    _summary(f"### New plan\n```\n{planner.describe(queue)}\n```\n")
    return 0


def _status(args: argparse.Namespace) -> int:
    cfg, cache = _load(args)
    queue = state.load_queue(cfg.queue_path)
    if queue is None:
        print("no queue yet — run `python main.py plan` or just `run`")
        print(f"cache: {len(cache)} fingerprints")
        return 0
    print(planner.describe(queue))
    print(f"cache: {len(cache)} fingerprints at {cfg.cache_path}")
    return 0


def _preview(args: argparse.Namespace) -> int:
    cfg, cache = _load(args)
    now = time.time()
    queue = state.load_queue(cfg.queue_path) or planner.build_queue(cfg, cache, now=now)
    by_key = {c.key: c for c in cfg.channels}
    shown = 0
    for plan in queue.channels:
        pending = plan.pending
        if not pending:
            continue
        batch = pending[0]
        configs = [_restamp(u, plan.username, cfg.cleaning) for u in batch.configs]
        channel = by_key.get(plan.key)
        extra = cfg.extra_text_for(channel) if channel else ""
        for text in tg.render(
            configs, plan.username, cfg.message, batch.index, len(plan.batches), extra
        ):
            print("=" * 72)
            print(f"# {plan.username} — batch {batch.index}, due {planner._ts(batch.due_at)}")
            print("=" * 72)
            print(text)
            shown += 1
    if not shown:
        print("nothing pending to preview")
    return 0


def _reset(args: argparse.Namespace) -> int:
    cfg, _ = _load(args)
    if cfg.queue_path.exists():
        cfg.queue_path.unlink()
        print(f"removed {cfg.queue_path}")
    else:
        print("no queue file to remove")
    return 0


def _clear_cache(args: argparse.Namespace) -> int:
    cfg, _ = _load(args)
    if not args.yes:
        print("refusing to clear the cache without --yes (every config could be re-sent)")
        return 1
    if cfg.cache_path.exists():
        cfg.cache_path.unlink()
        print(f"removed {cfg.cache_path}")
    else:
        print("no cache file to remove")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send V2Ray configs to Telegram channels on a schedule.")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "plan", "status", "preview", "reset", "clear-cache"])
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--dry-run", action="store_true", help="do everything except talk to Telegram")
    parser.add_argument("--rebuild", action="store_true", help="discard the current queue and re-plan")
    parser.add_argument("--yes", action="store_true", help="confirm destructive commands")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)

    if args.command == "run":
        return asyncio.run(_run(args))
    if args.command == "plan":
        return _plan(args)
    if args.command == "status":
        return _status(args)
    if args.command == "preview":
        return _preview(args)
    if args.command == "reset":
        return _reset(args)
    return _clear_cache(args)


if __name__ == "__main__":
    sys.exit(main())
