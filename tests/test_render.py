"""Tests for message rendering and splitting (no network access)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sender.tg import TELEGRAM_HARD_LIMIT, Credentials, MissingCredentials, render  # noqa: E402

MSG = {
    "header": "🌐 <b>V2Ray</b> — {date}\n📦 Batch {batch_index}/{batch_total} • {count} configs\n",
    "footer": "\n🔗 {channel}",
    "code_block": True,
    "max_chars": 4000,
}


def configs(n: int, length: int = 80) -> list[str]:
    return [f"vless://uuid-{i}@1.2.3.4:443?type=tcp#@Ch".ljust(length, "x") for i in range(n)]


class TestRender(unittest.TestCase):
    def test_single_message_for_small_batch(self):
        out = render(configs(10), "@MyChannel", MSG, 1, 10)
        self.assertEqual(len(out), 1)
        self.assertIn("<pre>", out[0])
        self.assertIn("@MyChannel", out[0])
        self.assertIn("Batch 1/10", out[0])
        self.assertIn("10 configs", out[0])

    def test_all_configs_in_one_code_block(self):
        out = render(configs(10), "@Ch", MSG, 1, 1)
        self.assertEqual(out[0].count("<pre>"), 1)
        self.assertEqual(out[0].count("</pre>"), 1)

    def test_every_config_present(self):
        items = configs(10)
        out = render(items, "@Ch", MSG, 1, 1)
        joined = "".join(out)
        for item in items:
            self.assertIn(item, joined)

    def test_html_is_escaped(self):
        out = render(["vless://u@1.2.3.4:443?a=1&b=2#<b>x</b>"], "@Ch", MSG, 1, 1)
        self.assertIn("&amp;", out[0])
        self.assertIn("&lt;b&gt;", out[0])

    def test_long_batch_is_split(self):
        out = render(configs(40, length=300), "@Ch", MSG, 1, 1)
        self.assertGreater(len(out), 1)
        for text in out:
            self.assertLessEqual(len(text), MSG["max_chars"])

    def test_split_keeps_every_config(self):
        items = configs(40, length=300)
        out = render(items, "@Ch", MSG, 1, 1)
        joined = "".join(out)
        for item in items:
            self.assertIn(item, joined)

    def test_split_parts_are_labelled(self):
        out = render(configs(40, length=300), "@Ch", MSG, 2, 5)
        self.assertIn("part 1/", out[0])
        self.assertIn(f"part {len(out)}/{len(out)}", out[-1])

    def test_max_chars_capped_at_telegram_limit(self):
        cfg = dict(MSG, max_chars=99_999)
        out = render(configs(200, length=200), "@Ch", cfg, 1, 1)
        for text in out:
            self.assertLessEqual(len(text), TELEGRAM_HARD_LIMIT)

    def test_plain_mode_without_code_block(self):
        out = render(configs(3), "@Ch", dict(MSG, code_block=False), 1, 1)
        self.assertNotIn("<pre>", out[0])

    def test_no_header_or_footer(self):
        out = render(configs(2), "@Ch", dict(MSG, header="", footer=""), 1, 1)
        self.assertTrue(out[0].startswith("<pre>"))
        self.assertTrue(out[0].endswith("</pre>"))

    def test_single_oversized_config_still_returned(self):
        # One config longer than the limit cannot be split further; it must not
        # silently vanish.
        giant = "vless://" + "x" * 5000
        out = render([giant], "@Ch", MSG, 1, 1)
        self.assertEqual(len(out), 1)
        self.assertIn("x" * 100, out[0])


class TestExtraText(unittest.TestCase):
    """The optional free text appended at the very end of a post."""

    def test_extra_text_appended_last(self):
        out = render(configs(3), "@Ch", MSG, 1, 1, "📣 هر روز کانفیگ تازه")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].rstrip().endswith("📣 هر روز کانفیگ تازه"))
        self.assertIn("</pre>", out[0])
        # Order: body, then footer, then extra text.
        self.assertLess(out[0].index("</pre>"), out[0].index("📣"))
        self.assertLess(out[0].index("🔗 @Ch"), out[0].index("📣"))

    def test_absent_by_default(self):
        out = render(configs(3), "@Ch", MSG, 1, 1)
        self.assertTrue(out[0].rstrip().endswith("@Ch"))

    def test_empty_string_adds_nothing(self):
        with_empty = render(configs(3), "@Ch", MSG, 1, 1, "")
        without = render(configs(3), "@Ch", MSG, 1, 1)
        self.assertEqual(with_empty, without)

    def test_read_from_message_config(self):
        cfg = dict(MSG, extra_text="powered by me")
        out = render(configs(3), "@Ch", cfg, 1, 1)
        self.assertIn("powered by me", out[0])

    def test_argument_overrides_config(self):
        cfg = dict(MSG, extra_text="global text")
        out = render(configs(3), "@Ch", cfg, 1, 1, "per-channel text")
        self.assertIn("per-channel text", out[0])
        self.assertNotIn("global text", out[0])

    def test_placeholders_are_filled(self):
        out = render(configs(4), "@MyCh", MSG, 2, 5, "join {channel} — {count} configs on {date}")
        self.assertIn("join @MyCh — 4 configs on", out[0])

    def test_unknown_placeholder_does_not_crash(self):
        out = render(configs(2), "@Ch", MSG, 1, 1, "price {price} toman {unclosed")
        self.assertIn("{price}", out[0])
        self.assertIn("{unclosed", out[0])

    def test_html_in_extra_text_is_preserved(self):
        # Unlike configs, the extra text is authored by the user, so its markup
        # must reach Telegram intact.
        out = render(configs(2), "@Ch", MSG, 1, 1, '<b>bold</b> <a href="https://t.me/x">link</a>')
        self.assertIn("<b>bold</b>", out[0])
        self.assertIn('<a href="https://t.me/x">link</a>', out[0])

    def test_only_on_final_part_when_split(self):
        out = render(configs(40, length=300), "@Ch", MSG, 1, 1, "CLOSING LINE")
        self.assertGreater(len(out), 1)
        for text in out[:-1]:
            self.assertNotIn("CLOSING LINE", text)
        self.assertIn("CLOSING LINE", out[-1])

    def test_split_respects_limit_with_extra_text(self):
        long_extra = "پیام تبلیغاتی بسیار طولانی " * 20
        out = render(configs(40, length=300), "@Ch", MSG, 1, 1, long_extra)
        for text in out:
            self.assertLessEqual(len(text), MSG["max_chars"])

    def test_multiline_extra_text(self):
        extra = "line one\nline two\nline three"
        out = render(configs(2), "@Ch", MSG, 1, 1, extra)
        self.assertIn(extra, out[0])


class TestCredentials(unittest.TestCase):
    def test_missing_env_raises(self):
        import os
        saved = {k: os.environ.pop(k, None) for k in
                 ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION")}
        try:
            with self.assertRaises(MissingCredentials):
                Credentials.from_env()
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_non_numeric_api_id_raises(self):
        import os
        saved = {k: os.environ.get(k) for k in
                 ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION")}
        os.environ.update({"TELEGRAM_API_ID": "abc", "TELEGRAM_API_HASH": "h",
                           "TELEGRAM_SESSION": "s"})
        try:
            with self.assertRaises(MissingCredentials):
                Credentials.from_env()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_valid_env(self):
        import os
        saved = {k: os.environ.get(k) for k in
                 ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION")}
        os.environ.update({"TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "hash",
                           "TELEGRAM_SESSION": "session"})
        try:
            creds = Credentials.from_env()
            self.assertEqual(creds.api_id, 12345)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main(verbosity=2)
