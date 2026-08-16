"""Tests for the cache, distribution, and time-slot scheduling.

    python tests/test_planner.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sender import planner, state  # noqa: E402
from sender.cache import Cache  # noqa: E402
from sender.settings import Channel, Settings, load  # noqa: E402


def make_settings(tmp: Path, **overrides) -> Settings:
    """Build a Settings object backed by a temporary directory."""
    from sender.settings import DEFAULTS, _deep_merge

    raw = _deep_merge(DEFAULTS, overrides)
    channels = [
        Channel(username=c["username"], daily_quota=int(c["daily_quota"]))
        for c in raw.get("channels") or []
    ]
    return Settings(raw=raw, root=tmp, channels=channels)


def write_source(tmp: Path, count: int, name: str = "working.txt") -> Path:
    """Write *count* distinct vless configs."""
    lines = [
        f"vless://uuid-{i}@10.0.{i // 250}.{i % 250}:443?type=tcp&security=reality#🔥Join+Telegram:@Ad"
        for i in range(count)
    ]
    path = tmp / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestCache(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            cache = Cache(path)
            cache.add("abc", "@ch")
            cache.add("def", "@ch")
            self.assertTrue(cache.save())

            reloaded = Cache(path)
            self.assertEqual(len(reloaded), 2)
            self.assertIn("abc", reloaded)
            self.assertTrue(reloaded.has("def"))

    def test_no_write_when_clean(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Cache(Path(td) / "cache.json")
            cache.add("a")
            cache.save()
            self.assertFalse(cache.save())  # nothing changed since

    def test_ttl_expiry(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            old = time.time() - 40 * 86400
            path.write_text(json.dumps({
                "version": 1,
                "entries": {"old": {"ts": old}, "new": {"ts": time.time()}},
            }), encoding="utf-8")
            cache = Cache(path, ttl_days=30)
            self.assertNotIn("old", cache)
            self.assertIn("new", cache)

    def test_ttl_zero_keeps_everything(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            path.write_text(json.dumps({
                "version": 1, "entries": {"ancient": {"ts": 1.0}},
            }), encoding="utf-8")
            self.assertIn("ancient", Cache(path, ttl_days=0))

    def test_max_entries_trim_drops_oldest(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            cache = Cache(path, max_entries=3)
            base = time.time() - 3600
            for i in range(6):
                cache.entries[f"k{i}"] = {"ts": base + i, "channel": "", "hits": 1}
            cache._dirty = True
            cache.save()
            reloaded = Cache(path, max_entries=3)
            self.assertEqual(len(reloaded), 3)
            self.assertIn("k5", reloaded)
            self.assertNotIn("k0", reloaded)

    def test_corrupt_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(len(Cache(path)), 0)

    def test_legacy_list_format(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            path.write_text(json.dumps(["a", "b"]), encoding="utf-8")
            cache = Cache(path)
            self.assertEqual(len(cache), 2)
            self.assertIn("a", cache)


class TestPrepare(unittest.TestCase):
    def test_dedup_and_cache_filtering(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            source = tmp / "working.txt"
            base = "vless://u1@1.2.3.4:443?type=tcp&security=reality"
            source.write_text("\n".join([
                base + "#@AdOne",              # kept
                base + "#@AdTwo",              # same server → duplicate remark only
                "vless://u2@5.6.7.8:443?type=tcp#@AdThree",
                "garbage line",                # invalid
                "",                            # blank
            ]), encoding="utf-8")

            cfg = make_settings(tmp, channels=[{"username": "@a", "daily_quota": 10}])
            cache = Cache(tmp / "cache.json")
            items, stats = planner.prepare(planner.load_source(cfg), cfg, cache)

            self.assertEqual(len(items), 2)
            self.assertEqual(stats["dup_in_batch"], 1)
            self.assertEqual(stats["invalid"], 1)

    def test_cached_config_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 5)
            cfg = make_settings(tmp, channels=[{"username": "@a", "daily_quota": 10}])
            cache = Cache(tmp / "cache.json")

            first, _ = planner.prepare(planner.load_source(cfg), cfg, cache)
            for item in first:
                cache.add(item.fp, "@a")

            second, stats = planner.prepare(planner.load_source(cfg), cfg, cache)
            self.assertEqual(second, [])
            self.assertEqual(stats["cached"], 5)

    def test_placeholder_remark_is_channel_independent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 3)
            cfg = make_settings(tmp, channels=[{"username": "@a", "daily_quota": 10}])
            items, _ = planner.prepare(planner.load_source(cfg), cfg, Cache(tmp / "c.json"))
            for item in items:
                self.assertIn("__PLACEHOLDER__", item.uri)


class TestDistribute(unittest.TestCase):
    def _settings(self, tmp: Path, quotas: list[int], mode: str = "round_robin") -> Settings:
        return make_settings(
            tmp,
            channels=[{"username": f"@ch{i}", "daily_quota": q} for i, q in enumerate(quotas)],
            distribution={"mode": mode, "shuffle": False, "seed": 42},
        )

    def _items(self, n: int) -> list[planner.Prepared]:
        return [planner.Prepared(uri=f"u{i}", fp=f"f{i}", endpoint=f"h{i % 7}:443") for i in range(n)]

    def test_even_three_way_split(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._settings(Path(td), [100, 100, 100])
            result = cfg and planner.distribute(self._items(99), cfg.channels, cfg)
            self.assertEqual([len(v) for v in result.values()], [33, 33, 33])

    def test_quota_is_a_ceiling(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._settings(Path(td), [5, 100, 100])
            result = planner.distribute(self._items(500), cfg.channels, cfg)
            # More items than total capacity, so every channel fills up exactly.
            self.assertEqual([len(v) for v in result.values()], [5, 100, 100])

    def test_small_quota_does_not_starve_others(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._settings(Path(td), [5, 100, 100])
            result = planner.distribute(self._items(200), cfg.channels, cfg)
            # ch0 stops at its quota; the rest is shared by the other two.
            self.assertEqual(len(result["ch0"]), 5)
            self.assertEqual(sum(len(v) for v in result.values()), 200)
            self.assertTrue(all(len(v) <= q for v, q in
                                zip(result.values(), [5, 100, 100])))

    def test_no_config_sent_to_two_channels(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._settings(Path(td), [50, 50, 50])
            result = planner.distribute(self._items(120), cfg.channels, cfg)
            all_fps = [item.fp for group in result.values() for item in group]
            self.assertEqual(len(all_fps), len(set(all_fps)))

    def test_fewer_items_than_channels(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._settings(Path(td), [10, 10, 10])
            result = planner.distribute(self._items(2), cfg.channels, cfg)
            self.assertEqual(sum(len(v) for v in result.values()), 2)

    def test_chunk_mode_gives_contiguous_slices(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._settings(Path(td), [3, 3, 3], mode="chunk")
            result = planner.distribute(self._items(9), cfg.channels, cfg)
            self.assertEqual([i.fp for i in result["ch0"]], ["f0", "f1", "f2"])
            self.assertEqual([i.fp for i in result["ch1"]], ["f3", "f4", "f5"])

    def test_empty_pool(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._settings(Path(td), [10, 10])
            result = planner.distribute([], cfg.channels, cfg)
            self.assertEqual(sum(len(v) for v in result.values()), 0)


class TestSlotting(unittest.TestCase):
    def test_hundred_configs_over_24h(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 300)
            cfg = make_settings(
                tmp,
                channels=[{"username": f"@ch{i}", "daily_quota": 100} for i in range(3)],
                schedule={"window_hours": 24, "batch_size": 10, "grace_minutes": 10,
                          "max_messages_per_run": 6, "inter_message_delay_seconds": 0,
                          "rebuild_when_exhausted": True},
                distribution={"mode": "round_robin", "shuffle": False, "seed": 1},
            )
            now = 1_700_000_000.0
            queue = planner.build_queue(cfg, Cache(tmp / "c.json"), now=now)

            self.assertEqual(len(queue.channels), 3)
            for plan in queue.channels:
                self.assertEqual(plan.total_configs, 100)
                self.assertEqual(len(plan.batches), 10)
                # First batch is immediate, the last still inside the window.
                self.assertAlmostEqual(plan.batches[0].due_at, now, places=3)
                self.assertLess(plan.batches[-1].due_at, queue.window_end)
                gaps = [
                    plan.batches[i + 1].due_at - plan.batches[i].due_at
                    for i in range(len(plan.batches) - 1)
                ]
                for gap in gaps:
                    self.assertAlmostEqual(gap, 24 * 3600 / 10, places=3)

    def test_only_first_batch_is_due_immediately(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 60)
            cfg = make_settings(
                tmp,
                channels=[{"username": "@a", "daily_quota": 60}],
                schedule={"window_hours": 24, "batch_size": 10, "grace_minutes": 0,
                          "max_messages_per_run": 6, "inter_message_delay_seconds": 0,
                          "rebuild_when_exhausted": True},
                distribution={"mode": "round_robin", "shuffle": False, "seed": 1},
            )
            now = 1_700_000_000.0
            queue = planner.build_queue(cfg, Cache(tmp / "c.json"), now=now)
            plan = queue.channels[0]
            self.assertEqual(len(plan.due(now)), 1)
            # Four hours in, exactly two of the six 4-hourly batches are due.
            self.assertEqual(len(plan.due(now + 4 * 3600 + 1)), 2)
            self.assertEqual(len(plan.due(now + 24 * 3600)), 6)

    def test_grace_window_pulls_next_batch_in(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 20)
            cfg = make_settings(
                tmp,
                channels=[{"username": "@a", "daily_quota": 20}],
                schedule={"window_hours": 2, "batch_size": 10, "grace_minutes": 10,
                          "max_messages_per_run": 6, "inter_message_delay_seconds": 0,
                          "rebuild_when_exhausted": True},
                distribution={"mode": "round_robin", "shuffle": False, "seed": 1},
            )
            now = 1_700_000_000.0
            queue = planner.build_queue(cfg, Cache(tmp / "c.json"), now=now)
            plan = queue.channels[0]
            # Second batch is due at +1h; with 10 min grace it is due at +50m.
            self.assertEqual(len(plan.due(now + 50 * 60, 600)), 2)
            self.assertEqual(len(plan.due(now + 45 * 60, 600)), 1)

    def test_batch_size_one(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 24)
            cfg = make_settings(
                tmp,
                channels=[{"username": "@a", "daily_quota": 24}],
                schedule={"window_hours": 24, "batch_size": 1, "grace_minutes": 0,
                          "max_messages_per_run": 6, "inter_message_delay_seconds": 0,
                          "rebuild_when_exhausted": True},
                distribution={"mode": "round_robin", "shuffle": False, "seed": 1},
            )
            queue = planner.build_queue(cfg, Cache(tmp / "c.json"), now=1_700_000_000.0)
            plan = queue.channels[0]
            self.assertEqual(len(plan.batches), 24)
            self.assertTrue(all(len(b.configs) == 1 for b in plan.batches))

    def test_partial_last_batch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 25)
            cfg = make_settings(
                tmp,
                channels=[{"username": "@a", "daily_quota": 25}],
                schedule={"window_hours": 24, "batch_size": 10, "grace_minutes": 0,
                          "max_messages_per_run": 6, "inter_message_delay_seconds": 0,
                          "rebuild_when_exhausted": True},
                distribution={"mode": "round_robin", "shuffle": False, "seed": 1},
            )
            queue = planner.build_queue(cfg, Cache(tmp / "c.json"), now=1_700_000_000.0)
            sizes = [len(b.configs) for b in queue.channels[0].batches]
            self.assertEqual(sizes, [10, 10, 5])

    def test_empty_source_produces_empty_queue(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "working.txt").write_text("", encoding="utf-8")
            cfg = make_settings(tmp, channels=[{"username": "@a", "daily_quota": 10}])
            queue = planner.build_queue(cfg, Cache(tmp / "c.json"), now=1_700_000_000.0)
            self.assertEqual(queue.total_configs, 0)
            self.assertTrue(queue.exhausted(1_700_000_000.0))


class TestQueuePersistence(unittest.TestCase):
    def _queue(self, tmp: Path) -> tuple[Settings, planner.Queue]:
        write_source(tmp, 30)
        cfg = make_settings(
            tmp,
            channels=[{"username": "@a", "daily_quota": 30}],
            schedule={"window_hours": 24, "batch_size": 10, "grace_minutes": 0,
                      "max_messages_per_run": 6, "inter_message_delay_seconds": 0,
                      "rebuild_when_exhausted": True},
            distribution={"mode": "round_robin", "shuffle": False, "seed": 1},
        )
        return cfg, planner.build_queue(cfg, Cache(tmp / "c.json"), now=1_700_000_000.0)

    def test_roundtrip_preserves_sent_state(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg, queue = self._queue(tmp)
            queue.channels[0].batches[0].sent_at = 123.0
            queue.channels[0].batches[0].message_id = 999
            state.save_queue(cfg.queue_path, queue)

            reloaded = state.load_queue(cfg.queue_path)
            self.assertIsNotNone(reloaded)
            first = reloaded.channels[0].batches[0]
            self.assertTrue(first.sent)
            self.assertEqual(first.message_id, 999)
            self.assertEqual(reloaded.pending_count, 2)

    def test_version_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "queue.json"
            path.write_text(json.dumps({"version": 999, "window_start": 0,
                                        "window_end": 1, "channels": []}), encoding="utf-8")
            self.assertIsNone(state.load_queue(path))

    def test_missing_file(self):
        self.assertIsNone(state.load_queue(Path(tempfile.gettempdir()) / "does-not-exist.json"))

    def test_exhausted_and_expired(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _, queue = self._queue(tmp)
            now = queue.window_start
            self.assertFalse(queue.exhausted(now))
            for plan in queue.channels:
                for batch in plan.batches:
                    batch.sent_at = now
            self.assertTrue(queue.exhausted(now))
            self.assertFalse(queue.expired(now))
            self.assertTrue(queue.expired(queue.window_end + 1))


class TestSettingsOverrides(unittest.TestCase):
    def test_env_override_types(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "config.json").write_text(json.dumps({
                "channels": [{"username": "@a", "daily_quota": 10}],
            }), encoding="utf-8")
            os.environ["V2TG_SCHEDULE__BATCH_SIZE"] = "25"
            os.environ["V2TG_RUNTIME__DRY_RUN"] = "true"
            os.environ["V2TG_CLEANING__AGGRESSIVE"] = "1"
            try:
                cfg = load(tmp / "config.json", root=tmp)
                self.assertEqual(cfg.schedule["batch_size"], 25)
                self.assertTrue(cfg.dry_run)
                self.assertTrue(cfg.cleaning["aggressive"])
            finally:
                for key in ("V2TG_SCHEDULE__BATCH_SIZE", "V2TG_RUNTIME__DRY_RUN",
                            "V2TG_CLEANING__AGGRESSIVE"):
                    os.environ.pop(key, None)

    def test_channels_as_json_env(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "config.json").write_text(json.dumps({
                "channels": [{"username": "@a", "daily_quota": 10}],
            }), encoding="utf-8")
            os.environ["V2TG_CHANNELS"] = json.dumps(
                [{"username": "b", "daily_quota": 5}, {"username": "@c", "daily_quota": 7}]
            )
            try:
                cfg = load(tmp / "config.json", root=tmp)
                self.assertEqual([c.username for c in cfg.channels], ["@b", "@c"])
                self.assertEqual(cfg.total_quota, 12)
            finally:
                os.environ.pop("V2TG_CHANNELS", None)

    def test_no_channels_is_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "config.json").write_text(json.dumps({"channels": []}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                load(tmp / "config.json", root=tmp)

    def test_duplicate_channels_collapsed(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "config.json").write_text(json.dumps({
                "channels": [{"username": "@a", "daily_quota": 10},
                             {"username": "A", "daily_quota": 20}],
            }), encoding="utf-8")
            cfg = load(tmp / "config.json", root=tmp)
            self.assertEqual(len(cfg.channels), 1)


class TestRecycling(unittest.TestCase):
    """Reusing already-sent configs once the source stops bringing new ones."""

    def _settings(self, tmp: Path, quota: int = 20, **recycle) -> Settings:
        return make_settings(
            tmp,
            channels=[{"username": "@a", "daily_quota": quota}],
            schedule={"window_hours": 24, "batch_size": 5, "grace_minutes": 0,
                      "max_messages_per_run": 6, "inter_message_delay_seconds": 0,
                      "rebuild_when_exhausted": True},
            distribution={"mode": "round_robin", "shuffle": False, "seed": 1},
            recycle={"enabled": True, "cooldown_hours": 0, "max_times_each": 0, **recycle},
        )

    def _seed_cache(self, tmp: Path, items, ages: dict[str, float] | None = None) -> Cache:
        """Mark *items* as already sent, optionally with a specific last-sent age."""
        cache = Cache(tmp / "cache.json")
        now = time.time()
        for index, item in enumerate(items):
            cache.add(item.fp, "@a")
            if ages and item.fp in ages:
                cache.entries[item.fp]["last"] = now - ages[item.fp]
            else:
                cache.entries[item.fp]["last"] = now - (len(items) - index) * 3600
        return cache

    def test_all_sent_source_still_fills_the_quota(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 20)
            cfg = self._settings(tmp)
            items, _, _ = planner._prepare_split(planner.load_source(cfg), cfg, Cache(tmp / "c.json"))
            cache = self._seed_cache(tmp, items)

            queue = planner.build_queue(cfg, cache, now=1_700_000_000.0)
            self.assertEqual(queue.total_configs, 20)
            self.assertEqual(queue.stats["recycled"], 20)
            self.assertEqual(queue.stats["ready"], 0)

    def test_fresh_configs_are_preferred_and_only_the_gap_is_filled(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 20)
            cfg = self._settings(tmp)
            items, _, _ = planner._prepare_split(planner.load_source(cfg), cfg, Cache(tmp / "c.json"))
            # 15 of the 20 have already gone out; 5 are new.
            cache = self._seed_cache(tmp, items[:15])

            queue = planner.build_queue(cfg, cache, now=1_700_000_000.0)
            self.assertEqual(queue.stats["ready"], 5)
            self.assertEqual(queue.stats["recycled"], 15)
            self.assertEqual(queue.total_configs, 20)

    def test_no_recycling_when_fresh_meets_the_quota(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 40)
            cfg = self._settings(tmp, quota=20)
            queue = planner.build_queue(cfg, Cache(tmp / "c.json"), now=1_700_000_000.0)
            self.assertEqual(queue.stats["recycled"], 0)
            self.assertEqual(queue.total_configs, 20)

    def test_oldest_sent_comes_back_first(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 10)
            cfg = self._settings(tmp, quota=3)
            items, _, _ = planner._prepare_split(planner.load_source(cfg), cfg, Cache(tmp / "c.json"))
            # item[0] sent 100h ago, item[1] 90h ago, ... item[9] 10h ago.
            ages = {item.fp: (10 - i) * 10 * 3600 for i, item in enumerate(items)}
            cache = self._seed_cache(tmp, items, ages)

            queue = planner.build_queue(cfg, cache, now=time.time())
            scheduled = [c for b in queue.channels[0].batches for c in b.configs]
            self.assertEqual(scheduled, [items[0].uri, items[1].uri, items[2].uri])

    def test_disabled_means_no_recycling(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 10)
            cfg = self._settings(tmp, enabled=False)
            items, _, _ = planner._prepare_split(planner.load_source(cfg), cfg, Cache(tmp / "c.json"))
            cache = self._seed_cache(tmp, items)

            queue = planner.build_queue(cfg, cache, now=1_700_000_000.0)
            self.assertEqual(queue.total_configs, 0)
            self.assertEqual(queue.stats["recycled"], 0)

    def test_cooldown_blocks_recent_configs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 6)
            cfg = self._settings(tmp, cooldown_hours=48)
            items, _, _ = planner._prepare_split(planner.load_source(cfg), cfg, Cache(tmp / "c.json"))
            # First three are old enough; the rest are inside the cooldown.
            ages = {item.fp: (72 if i < 3 else 2) * 3600 for i, item in enumerate(items)}
            cache = self._seed_cache(tmp, items, ages)

            queue = planner.build_queue(cfg, cache, now=time.time())
            self.assertEqual(queue.stats["recycled"], 3)

    def test_max_times_each_caps_repeats(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 5)
            cfg = self._settings(tmp, max_times_each=2)
            items, _, _ = planner._prepare_split(planner.load_source(cfg), cfg, Cache(tmp / "c.json"))
            cache = self._seed_cache(tmp, items)
            # Two of them have already been sent twice, so they are used up.
            for item in items[:2]:
                cache.entries[item.fp]["hits"] = 2

            queue = planner.build_queue(cfg, cache, now=time.time())
            self.assertEqual(queue.stats["recycled"], 3)

    def test_hits_and_last_are_tracked_across_sends(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache = Cache(tmp / "cache.json")
            cache.add("fp1", "@a")
            first_seen = cache.entries["fp1"]["ts"]
            self.assertEqual(cache.times_sent("fp1"), 1)

            cache.entries["fp1"]["last"] = first_seen - 7200
            cache.add("fp1", "@b")
            self.assertEqual(cache.times_sent("fp1"), 2)
            self.assertEqual(cache.entries["fp1"]["ts"], first_seen)
            self.assertGreater(cache.last_sent("fp1"), first_seen - 7200)

    def test_last_sent_defaults_for_legacy_entries(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "cache.json"
            path.write_text(json.dumps({
                "version": 1, "entries": {"old": {"ts": time.time() - 3600}},
            }), encoding="utf-8")
            cache = Cache(path)
            # No "last" field in the legacy file, so it falls back to "ts".
            self.assertAlmostEqual(cache.last_sent("old"), cache.entries["old"]["ts"], places=3)
            self.assertEqual(cache.last_sent("missing"), 0.0)


class TestExtraTextResolution(unittest.TestCase):
    """Per-channel extra_text overriding the global message.extra_text."""

    def _load(self, tmp: Path, payload: dict) -> Settings:
        (tmp / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        return load(tmp / "config.json", root=tmp)

    def test_global_applies_to_all_channels(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._load(tmp, {
                "channels": [{"username": "@a", "daily_quota": 10},
                             {"username": "@b", "daily_quota": 10}],
                "message": {"extra_text": "global line"},
            })
            for channel in cfg.channels:
                self.assertEqual(cfg.extra_text_for(channel), "global line")

    def test_channel_value_wins(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._load(tmp, {
                "channels": [{"username": "@a", "daily_quota": 10, "extra_text": "mine"},
                             {"username": "@b", "daily_quota": 10}],
                "message": {"extra_text": "global line"},
            })
            by_name = {c.username: c for c in cfg.channels}
            self.assertEqual(cfg.extra_text_for(by_name["@a"]), "mine")
            self.assertEqual(cfg.extra_text_for(by_name["@b"]), "global line")

    def test_empty_string_silences_the_global_text(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._load(tmp, {
                "channels": [{"username": "@a", "daily_quota": 10, "extra_text": ""}],
                "message": {"extra_text": "global line"},
            })
            self.assertEqual(cfg.extra_text_for(cfg.channels[0]), "")

    def test_list_of_lines_is_joined(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._load(tmp, {
                "channels": [{"username": "@a", "daily_quota": 10}],
                "message": {"extra_text": ["first", "second", "third"]},
            })
            self.assertEqual(cfg.extra_text_for(cfg.channels[0]), "first\nsecond\nthird")

    def test_channel_list_of_lines(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._load(tmp, {
                "channels": [{"username": "@a", "daily_quota": 10,
                              "extra_text": ["one", "two"]}],
            })
            self.assertEqual(cfg.extra_text_for(cfg.channels[0]), "one\ntwo")

    def test_absent_everywhere_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._load(tmp, {"channels": [{"username": "@a", "daily_quota": 10}]})
            self.assertEqual(cfg.extra_text_for(cfg.channels[0]), "")

    def test_env_override_of_global_text(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["V2TG_MESSAGE__EXTRA_TEXT"] = "from env"
            try:
                cfg = self._load(tmp, {"channels": [{"username": "@a", "daily_quota": 10}]})
                self.assertEqual(cfg.extra_text_for(cfg.channels[0]), "from env")
            finally:
                os.environ.pop("V2TG_MESSAGE__EXTRA_TEXT", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
