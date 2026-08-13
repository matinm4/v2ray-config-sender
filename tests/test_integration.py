"""End-to-end tests with Telegram stubbed out.

These exercise the real ``main._run`` loop — queue building, due-time selection,
cache writes, state persistence, and crash recovery — against a fake sender that
records what it would have posted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as main_mod  # noqa: E402
from sender import state  # noqa: E402
from sender.cache import Cache  # noqa: E402


class FakeSender:
    """Stand-in for ``sender.tg.Sender`` that records messages instead of sending."""

    instances: list["FakeSender"] = []

    def __init__(self, creds, dry_run: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_on: set[str] = set()
        self._next_id = 1000
        FakeSender.instances.append(self)

    async def __aenter__(self) -> "FakeSender":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def send(self, channel: str, text: str) -> int:
        if channel in self.fail_on:
            raise RuntimeError(f"simulated failure for {channel}")
        self.sent.append((channel, text))
        self._next_id += 1
        return self._next_id

    @property
    def configs_sent(self) -> list[str]:
        """Every config line across every recorded message."""
        out: list[str] = []
        for _channel, text in self.sent:
            body = text.split("<pre>", 1)[-1].split("</pre>", 1)[0]
            out.extend(line for line in body.splitlines() if "://" in line)
        return out


def write_source(tmp: Path, count: int) -> None:
    lines = [
        f"vless://uuid-{i}@10.{i // 250}.{i % 250}.5:443?type=tcp&security=reality"
        f"&sni=host{i}.example.com#🔥Join+Telegram:@AdChannel"
        for i in range(count)
    ]
    (tmp / "working.txt").write_text("\n".join(lines), encoding="utf-8")


def write_config(tmp: Path, **schedule) -> Path:
    cfg = {
        "source_file": "working.txt",
        "channels": [
            {"username": "@ChanOne", "daily_quota": 100},
            {"username": "@ChanTwo", "daily_quota": 100},
            {"username": "@ChanThree", "daily_quota": 100},
        ],
        "schedule": {
            "window_hours": 24,
            "batch_size": 10,
            "grace_minutes": 0,
            "max_messages_per_run": 6,
            "inter_message_delay_seconds": 0,
            "rebuild_when_exhausted": True,
            **schedule,
        },
        "distribution": {"mode": "round_robin", "shuffle": False, "seed": 7},
        "cache": {"file": "state/cache.json", "ttl_days": 30, "max_entries": 200000},
        "runtime": {"dry_run": False, "log_level": "WARNING", "state_dir": "state"},
    }
    path = tmp / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def run_once(tmp: Path, now: float | None = None, rebuild: bool = False) -> FakeSender:
    """Invoke the real run loop with a fake Telegram client at a fixed time."""
    args = Namespace(config="config.json", dry_run=False, rebuild=rebuild,
                     yes=False, log_level="WARNING", command="run")
    FakeSender.instances.clear()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(main_mod, "ROOT", tmp))
        stack.enter_context(mock.patch.object(main_mod.tg, "Sender", FakeSender))
        stack.enter_context(mock.patch.object(
            main_mod.tg.Credentials, "from_env",
            classmethod(lambda cls: main_mod.tg.Credentials(1, "h", "s")),
        ))
        if now is not None:
            stack.enter_context(mock.patch("time.time", lambda: now))
        asyncio.run(main_mod._run(args))
    return FakeSender.instances[-1] if FakeSender.instances else FakeSender(None)


class TestEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        write_source(self.tmp, 300)
        write_config(self.tmp)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_first_run_sends_one_batch_per_channel(self):
        sender = run_once(self.tmp)
        channels = [c for c, _ in sender.sent]
        self.assertEqual(sorted(channels), ["@ChanOne", "@ChanThree", "@ChanTwo"])
        self.assertEqual(len(sender.configs_sent), 30)

    def test_each_config_carries_its_own_channel_name(self):
        sender = run_once(self.tmp)
        for channel, text in sender.sent:
            body = text.split("<pre>", 1)[-1].split("</pre>", 1)[0]
            for line in body.splitlines():
                if "://" in line:
                    self.assertTrue(line.rstrip().endswith(f"#{channel}"), line[-60:])

    def test_no_placeholder_leaks_into_messages(self):
        sender = run_once(self.tmp)
        for _channel, text in sender.sent:
            self.assertNotIn("__PLACEHOLDER__", text)

    def test_ads_removed_from_sent_configs(self):
        sender = run_once(self.tmp)
        for line in sender.configs_sent:
            self.assertNotIn("AdChannel", line)

    def test_second_run_immediately_after_sends_nothing(self):
        run_once(self.tmp)
        sender = run_once(self.tmp)
        self.assertEqual(sender.sent, [])

    def test_later_run_sends_the_next_batch(self):
        first = run_once(self.tmp)
        queue = state.load_queue(self.tmp / "state" / "queue.json")
        # Four hours in, batch 2 of each channel is due.
        second = run_once(self.tmp, now=queue.window_start + 4 * 3600 + 60)
        self.assertEqual(len(second.sent), 3)
        self.assertEqual(
            set(first.configs_sent) & set(second.configs_sent), set(),
            "a config was sent twice across runs",
        )

    def test_cache_prevents_resend_after_rebuild(self):
        first = run_once(self.tmp)
        sent_first = set(first.configs_sent)
        # Force a brand-new plan; the cache must still exclude what already went.
        second = run_once(self.tmp, rebuild=True)
        self.assertEqual(sent_first & set(second.configs_sent), set())

    def test_cache_file_grows_with_sent_configs(self):
        run_once(self.tmp)
        cache = Cache(self.tmp / "state" / "cache.json")
        self.assertEqual(len(cache), 30)
        run_once(self.tmp, rebuild=True)
        self.assertEqual(len(Cache(self.tmp / "state" / "cache.json")), 60)

    def test_no_config_reaches_two_channels(self):
        queue_path = self.tmp / "state" / "queue.json"
        seen: set[str] = set()
        run_once(self.tmp)
        queue = state.load_queue(queue_path)
        start = queue.window_start
        for hour in (4, 8, 12, 16, 20):
            sender = run_once(self.tmp, now=start + hour * 3600 + 60)
            for line in sender.configs_sent:
                core = line.split("#")[0]
                self.assertNotIn(core, seen, "same config went to more than one channel")
                seen.add(core)

    def test_sent_state_survives_a_crash_mid_run(self):
        # ChanTwo fails; ChanOne and ChanThree must still be recorded as sent.
        args = Namespace(config="config.json", dry_run=False, rebuild=False,
                         yes=False, log_level="WARNING", command="run")

        class FailingSender(FakeSender):
            def __init__(self, creds, dry_run=False):
                super().__init__(creds, dry_run)
                self.fail_on = {"@ChanTwo"}

        with mock.patch.object(main_mod, "ROOT", self.tmp), \
             mock.patch.object(main_mod.tg, "Sender", FailingSender), \
             mock.patch.object(main_mod.tg.Credentials, "from_env",
                               classmethod(lambda cls: main_mod.tg.Credentials(1, "h", "s"))):
            asyncio.run(main_mod._run(args))

        queue = state.load_queue(self.tmp / "state" / "queue.json")
        by_channel = {p.username: p for p in queue.channels}
        self.assertTrue(by_channel["@ChanOne"].batches[0].sent)
        self.assertFalse(by_channel["@ChanTwo"].batches[0].sent)
        self.assertTrue(by_channel["@ChanThree"].batches[0].sent)

    def test_failed_batch_is_retried_next_run(self):
        args = Namespace(config="config.json", dry_run=False, rebuild=False,
                         yes=False, log_level="WARNING", command="run")

        class FailingSender(FakeSender):
            def __init__(self, creds, dry_run=False):
                super().__init__(creds, dry_run)
                self.fail_on = {"@ChanTwo"}

        with mock.patch.object(main_mod, "ROOT", self.tmp), \
             mock.patch.object(main_mod.tg, "Sender", FailingSender), \
             mock.patch.object(main_mod.tg.Credentials, "from_env",
                               classmethod(lambda cls: main_mod.tg.Credentials(1, "h", "s"))):
            asyncio.run(main_mod._run(args))

        retry = run_once(self.tmp)
        self.assertEqual([c for c, _ in retry.sent], ["@ChanTwo"])

    def test_max_messages_per_run_is_respected(self):
        write_config(self.tmp, max_messages_per_run=2, window_hours=1, batch_size=10)
        run_once(self.tmp)
        queue = state.load_queue(self.tmp / "state" / "queue.json")
        # Well past the window: every batch is due, but the cap holds.
        sender = run_once(self.tmp, now=queue.window_start + 7200)
        self.assertEqual(len(sender.sent), 2)

    def test_quota_change_takes_effect_on_rebuild(self):
        run_once(self.tmp)
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        cfg["channels"] = [{"username": "@ChanOne", "daily_quota": 20}]
        (self.tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

        run_once(self.tmp, rebuild=True)
        queue = state.load_queue(self.tmp / "state" / "queue.json")
        self.assertEqual(len(queue.channels), 1)
        self.assertEqual(queue.channels[0].total_configs, 20)

    def test_exhausted_source_sends_nothing_but_does_not_crash(self):
        write_source(self.tmp, 5)
        write_config(self.tmp, batch_size=5)
        run_once(self.tmp)  # sends the only 5
        sender = run_once(self.tmp, rebuild=True)
        self.assertEqual(sender.sent, [])


class TestExtraTextEndToEnd(unittest.TestCase):
    """The optional closing text, all the way through a real run."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        write_source(self.tmp, 90)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _configure(self, message: dict, channels: list[dict] | None = None) -> None:
        write_config(self.tmp)
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        cfg["message"] = message
        if channels is not None:
            cfg["channels"] = channels
        (self.tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def test_global_extra_text_reaches_every_channel(self):
        self._configure({"header": "", "footer": "\n{channel}",
                         "extra_text": "📣 کانال ما را به دوستانتان معرفی کنید",
                         "code_block": True, "max_chars": 4000})
        sender = run_once(self.tmp)
        self.assertEqual(len(sender.sent), 3)
        for _channel, text in sender.sent:
            self.assertTrue(text.rstrip().endswith("معرفی کنید"), text[-80:])

    def test_per_channel_text_differs(self):
        self._configure(
            {"header": "", "footer": "", "extra_text": "GLOBAL",
             "code_block": True, "max_chars": 4000},
            channels=[
                {"username": "@ChanOne", "daily_quota": 30, "extra_text": "ONE ONLY"},
                {"username": "@ChanTwo", "daily_quota": 30, "extra_text": ""},
                {"username": "@ChanThree", "daily_quota": 30},
            ],
        )
        sender = run_once(self.tmp)
        got = {channel: text for channel, text in sender.sent}
        self.assertIn("ONE ONLY", got["@ChanOne"])
        self.assertNotIn("GLOBAL", got["@ChanOne"])
        self.assertNotIn("GLOBAL", got["@ChanTwo"])
        self.assertNotIn("ONE ONLY", got["@ChanTwo"])
        self.assertIn("GLOBAL", got["@ChanThree"])

    def test_extra_text_as_list_of_lines(self):
        self._configure({"header": "", "footer": "", "code_block": True, "max_chars": 4000,
                         "extra_text": ["🔴 line one", "🟢 line two"]})
        sender = run_once(self.tmp)
        for _channel, text in sender.sent:
            self.assertIn("🔴 line one\n🟢 line two", text)

    def test_html_and_placeholders_in_extra_text(self):
        self._configure({"header": "", "footer": "", "code_block": True, "max_chars": 4000,
                         "extra_text": '<b>{channel}</b> — <a href="https://t.me/x">more</a>'})
        sender = run_once(self.tmp)
        for channel, text in sender.sent:
            self.assertIn(f"<b>{channel}</b>", text)
            self.assertIn('<a href="https://t.me/x">more</a>', text)

    def test_configs_still_escaped_while_extra_text_is_not(self):
        self._configure({"header": "", "footer": "", "code_block": True, "max_chars": 4000,
                         "extra_text": "<i>promo</i>"})
        sender = run_once(self.tmp)
        for _channel, text in sender.sent:
            self.assertIn("&amp;", text.split("</pre>")[0] + "&amp;")
            self.assertIn("<i>promo</i>", text)


class TestDryRun(unittest.TestCase):
    def test_dry_run_writes_no_cache(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_source(tmp, 60)
            write_config(tmp)
            args = Namespace(config="config.json", dry_run=True, rebuild=False,
                             yes=False, log_level="WARNING", command="run")
            FakeSender.instances.clear()
            with mock.patch.object(main_mod, "ROOT", tmp), \
                 mock.patch.object(main_mod.tg, "Sender", FakeSender):
                asyncio.run(main_mod._run(args))
            self.assertFalse((tmp / "state" / "cache.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
