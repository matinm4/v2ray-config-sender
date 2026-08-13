"""Unit tests for cleaning, fingerprinting, distribution, and slotting.

    python -m pytest tests/ -q          # if pytest is installed
    python tests/test_cleaner.py        # plain-stdlib fallback
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sender.cleaner import clean, is_valid_host, looks_like_ad, strip_ads  # noqa: E402
from sender.fingerprint import endpoint_key, fingerprint  # noqa: E402

CFG = {"aggressive": False, "replace_remark": True, "remark_template": "{channel}", "strip_ad_params": True}
CFG_AGGRESSIVE = dict(CFG, aggressive=True)
CH = "@MyChannel"


def q(uri: str) -> dict:
    return parse_qs(urlparse(uri).query, keep_blank_values=True)


class TestRemark(unittest.TestCase):
    def test_remark_replaced_with_channel(self):
        raw = "vless://uuid@1.2.3.4:443?type=tcp#🔥Join+Telegram:@Farah_VPN🟣"
        out = clean(raw, CH, CFG).uri
        self.assertIsNotNone(out)
        self.assertTrue(out.endswith("#@MyChannel"), out)
        self.assertNotIn("Farah_VPN", out)

    def test_missing_remark_is_added(self):
        out = clean("vless://uuid@1.2.3.4:443?type=tcp", CH, CFG).uri
        self.assertIn("#@MyChannel", out)

    def test_percent_encoded_ad_remark_replaced(self):
        raw = "ss://YWVzOnB3@1.2.3.4:99#%F0%9F%87%A6%20%40ApexConfigVpn"
        out = clean(raw, CH, CFG).uri
        self.assertNotIn("ApexConfigVpn", out)
        self.assertIn("MyChannel", out)


class TestAdParams(unittest.TestCase):
    def test_telegram_named_param_removed(self):
        raw = ("vless://u@1.2.3.4:443?Telegram=@GozargahAzad,@GozargahAzad"
               "&security=reality&type=tcp#x")
        out = clean(raw, CH, CFG).uri
        self.assertNotIn("Gozargah", out)
        self.assertIn("security=reality", out)
        self.assertIn("type=tcp", out)

    def test_ad_only_host_dropped(self):
        raw = ("vless://u@1.2.3.4:443?host=BIA_TELEGRAM?=@ShadowFlux2---@ShadowFlux2"
               "---@ShadowFlux2&type=tcp&sni=real.example.com#x")
        out = clean(raw, CH, CFG).uri
        self.assertNotIn("ShadowFlux", out)
        self.assertEqual(q(out).get("sni"), ["real.example.com"])

    def test_real_host_kept(self):
        raw = "vless://u@1.2.3.4:443?host=play.google.com&type=tcp&headerType=http#x"
        out = clean(raw, CH, CFG).uri
        self.assertEqual(q(out)["host"], ["play.google.com"])

    def test_ad_stripped_from_mixed_host(self):
        raw = "vless://u@1.2.3.4:443?host=/?JOIN_TELEGRAM@MARAMBASHI_MARAMBASHI&sni=fr.sellflow.org&type=tcp#x"
        out = clean(raw, CH, CFG).uri
        self.assertNotIn("MARAMBASHI", out)
        self.assertEqual(q(out)["sni"], ["fr.sellflow.org"])

    def test_hostname_salvaged_from_polluted_value(self):
        raw = "vless://u@1.2.3.4:443?host=@AdChannel---cdn.real-site.com---@AdChannel&type=ws#x"
        out = clean(raw, CH, CFG).uri
        self.assertEqual(q(out)["host"], ["cdn.real-site.com"])

    def test_repeated_token_host_dropped(self):
        raw = "vless://u@1.2.3.4:443?host=v2rayNplus--v2rayNplus--v2rayNplus&type=tcp&security=tls#x"
        out = clean(raw, CH, CFG).uri
        self.assertNotIn("v2rayNplus", out)
        self.assertIn("security=tls", out)

    def test_malformed_param_name_dropped(self):
        raw = "vless://u@1.2.3.4:2083?type=tcp&f`p`=firefox&security=reality#x"
        out = clean(raw, CH, CFG).uri
        self.assertNotIn("firefox", out)
        self.assertIn("security=reality", out)

    def test_truncated_extra_json_dropped(self):
        raw = "vless://u@1.2.3.4:59595?type=xhttp&path=/&mode=auto&extra={"
        out = clean(raw, CH, CFG).uri
        self.assertNotIn("extra=", out)
        self.assertIn("type=xhttp", out)

    def test_valid_extra_json_kept(self):
        raw = 'vless://u@1.2.3.4:443?type=xhttp&extra=%7B%22noGRPCHeader%22%3Atrue%7D&security=tls#x'
        out = clean(raw, CH, CFG).uri
        self.assertIn("extra=", out)


class TestPaths(unittest.TestCase):
    def test_functional_path_preserved(self):
        raw = "vless://u@1.2.3.4:8880?path=/?ed=2082&type=httpupgrade&host=real.example.ir#x"
        out = clean(raw, CH, CFG).uri
        self.assertIn("ed=2082", out)

    def test_ad_suffix_cut_from_path(self):
        raw = ("vless://u@1.2.3.4:443?path=/api/v1/rooms/633/sync/?TELEGRAM-MARAMBASHI_MARAMBASHI?ed"
               "&type=ws&security=tls#x")
        out = clean(raw, CH, CFG).uri
        self.assertNotIn("MARAMBASHI", out)
        self.assertIn("/api/v1/rooms/633", out)

    def test_pure_ad_path_kept_in_smart_mode(self):
        # The whole path is the ad, so it is probably also the real route.
        raw = "vless://u@static.example.com:443?path=%2FJoin-Javidnaman-on-Telegram&type=xhttp&security=tls#x"
        out = clean(raw, CH, CFG).uri
        self.assertIn("Join-Javidnaman", out)

    def test_aggressive_mode_strips_ad_path(self):
        raw = "vless://u@static.example.com:443?path=%2FJoin-Javidnaman-on-Telegram&type=xhttp&security=tls#x"
        out = clean(raw, CH, CFG_AGGRESSIVE).uri
        self.assertNotIn("Javidnaman", out)
        self.assertIn("security=tls", out)


class TestVmess(unittest.TestCase):
    def _decode(self, uri: str) -> dict:
        import base64
        payload = uri.split("://", 1)[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.b64decode(payload).decode())

    def test_ps_replaced_and_host_ad_removed(self):
        import base64
        original = {
            "v": "2", "ps": "🔥Join Telegram:@Farah_VPN", "add": "1.2.3.4", "port": "443",
            "id": "abc", "net": "tcp", "host": "@VPNine1", "path": "/ @VPNine1", "tls": "",
        }
        raw = "vmess://" + base64.b64encode(json.dumps(original).encode()).decode()
        out = clean(raw, CH, CFG).uri
        data = self._decode(out)
        self.assertEqual(data["ps"], CH)
        self.assertEqual(data["host"], "")
        self.assertNotIn("VPNine1", json.dumps(data))
        self.assertEqual(data["add"], "1.2.3.4")

    def test_real_vmess_line_from_source(self):
        raw = ("vmess://eyJ2IjogIjIiLCAicHMiOiAiXHVkODNkXHVkZDI1Sm9pbitUZWxlZ3JhbTpARmFyYWhfVlBOXHVkODNk"
               "XHVkZmUzIiwgImFkZCI6ICIxNjUuMTQwLjIxNi4xNDEiLCAicG9ydCI6ICI0NDMiLCAiaWQiOiAiZTdkNzJhOGQt"
               "MjZmMi00YjU0LWIzNjYtMGM0M2UwYmNiYTdkIiwgImFpZCI6ICIwIiwgInNjeSI6ICJhdXRvIiwgIm5ldCI6ICJ0"
               "Y3AiLCAidHlwZSI6ICJub25lIiwgImhvc3QiOiAiIiwgInBhdGgiOiAiIiwgInRscyI6ICIiLCAic25pIjogIiIs"
               "ICJhbHBuIjogIiIsICJmcCI6ICIiLCAiaW5zZWN1cmUiOiAiMCIsICJ2Y24iOiAiIiwgInBjcyI6ICIifQ==")
        result = clean(raw, CH, CFG)
        self.assertTrue(result.ok)
        data = self._decode(result.uri)
        self.assertEqual(data["ps"], CH)
        self.assertEqual(data["add"], "165.140.216.141")

    def test_invalid_base64_rejected(self):
        self.assertFalse(clean("vmess://!!!not-base64!!!", CH, CFG).ok)

    def test_vmess_without_address_rejected(self):
        import base64
        raw = "vmess://" + base64.b64encode(json.dumps({"v": "2", "port": "443"}).encode()).decode()
        self.assertFalse(clean(raw, CH, CFG).ok)


class TestRejection(unittest.TestCase):
    def test_blank_and_comments(self):
        for line in ("", "   ", "# comment", "// note"):
            self.assertFalse(clean(line, CH, CFG).ok)

    def test_unknown_scheme(self):
        self.assertFalse(clean("ftp://1.2.3.4/x", CH, CFG).ok)

    def test_no_scheme(self):
        self.assertFalse(clean("just some text", CH, CFG).ok)

    def test_vless_without_uuid(self):
        self.assertFalse(clean("vless://1.2.3.4:443?type=tcp", CH, CFG).ok)


class TestSS(unittest.TestCase):
    def test_base64_ss_gets_remark(self):
        raw = "ss://MjAyMi1ibGFrZTMtYWVzLTI1Ni1nY206Zm52bzgycDl5NEFL@167.150.100.115:27755#🇸🇬"
        out = clean(raw, CH, CFG).uri
        self.assertIn("167.150.100.115:27755", out)
        self.assertIn("MyChannel", out)

    def test_fully_encoded_ss_normalized(self):
        import base64
        inner = "aes-256-gcm:password@1.2.3.4:8388"
        raw = "ss://" + base64.b64encode(inner.encode()).decode() + "#@AdChannel"
        out = clean(raw, CH, CFG).uri
        self.assertIn("1.2.3.4:8388", out)
        self.assertNotIn("AdChannel", out)


class TestHelpers(unittest.TestCase):
    def test_is_valid_host(self):
        for good in ("play.google.com", "1.2.3.4", "sub.domain.co.uk", "xn-de.dbsll.ir"):
            self.assertTrue(is_valid_host(good), good)
        for bad in ("", "@Channel", "BIA_TELEGRAM?=@x", "just_text", "/?JOIN@X"):
            self.assertFalse(is_valid_host(bad), bad)

    def test_looks_like_ad(self):
        self.assertTrue(looks_like_ad("@SomeChannel"))
        self.assertTrue(looks_like_ad("Join Telegram"))
        self.assertTrue(looks_like_ad("x--x--x--x"))
        self.assertFalse(looks_like_ad("play.google.com"))
        self.assertFalse(looks_like_ad("/ws/abc-123"))

    def test_strip_ads(self):
        self.assertEqual(strip_ads("@Ad---@Ad---@Ad"), "")
        self.assertNotIn("@", strip_ads("real.com @AdChannel"))
        # Too short to be a Telegram username, so left alone.
        self.assertIn("@x", strip_ads("keep @x"))


class TestFingerprint(unittest.TestCase):
    def test_remark_does_not_change_fingerprint(self):
        a = "vless://u@1.2.3.4:443?type=tcp&security=reality#@ChannelOne"
        b = "vless://u@1.2.3.4:443?type=tcp&security=reality#@ChannelTwo"
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_param_order_does_not_change_fingerprint(self):
        a = "vless://u@1.2.3.4:443?type=tcp&security=reality&sni=x.com#r"
        b = "vless://u@1.2.3.4:443?sni=x.com&security=reality&type=tcp#r"
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_cosmetic_params_ignored(self):
        a = "vless://u@1.2.3.4:443?type=tcp&fp=chrome&allowInsecure=1#r"
        b = "vless://u@1.2.3.4:443?type=tcp&fp=firefox#r"
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_different_server_differs(self):
        a = "vless://u@1.2.3.4:443?type=tcp#r"
        b = "vless://u@1.2.3.5:443?type=tcp#r"
        self.assertNotEqual(fingerprint(a), fingerprint(b))

    def test_different_port_differs(self):
        a = "vless://u@1.2.3.4:443?type=tcp#r"
        b = "vless://u@1.2.3.4:8443?type=tcp#r"
        self.assertNotEqual(fingerprint(a), fingerprint(b))

    def test_different_uuid_differs(self):
        a = "vless://uuid-a@1.2.3.4:443?type=tcp#r"
        b = "vless://uuid-b@1.2.3.4:443?type=tcp#r"
        self.assertNotEqual(fingerprint(a), fingerprint(b))

    def test_vmess_remark_ignored(self):
        import base64
        base = {"v": "2", "add": "1.2.3.4", "port": "443", "id": "abc", "net": "ws", "path": "/x"}
        a = "vmess://" + base64.b64encode(json.dumps(dict(base, ps="Ad One")).encode()).decode()
        b = "vmess://" + base64.b64encode(json.dumps(dict(base, ps="Ad Two")).encode()).decode()
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_endpoint_key(self):
        self.assertEqual(endpoint_key("vless://u@1.2.3.4:443?type=tcp#r"), "1.2.3.4:443")

    def test_garbage_returns_none(self):
        self.assertIsNone(fingerprint("not a uri"))
        self.assertIsNone(fingerprint(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
