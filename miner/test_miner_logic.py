#!/usr/bin/env python3
"""Headless tests for the v1.2.0 UI-support logic: output-line classification,
exit-code translation, CPU-mismatch detection, build preference order, and the
minetest address guard. Run: python3 test_miner_logic.py -v"""
import os
import tempfile
import unittest
import sololuck_miner as m


class TestClassifyLine(unittest.TestCase):
    def test_failures_are_fail(self):
        for line in (
            "[2026-07-10 01:00:00] Stratum connection failed: retry after 10 seconds",
            "stratum connection interrupted",
            "Stratum connection timed out",
            "...retry after 30 seconds...",
            "Failed to connect to sololuck.io",
            "stratum authentication failed",
        ):
            self.assertEqual(m.classify_line(line), "fail", line)

    def test_live_lines_are_live(self):
        for line in (
            "Stratum difficulty set to 1",
            "Stratum connection established",
            "sha256d: new work received",
            "New Block 903111, Net Diff 126.4T",
            "Threads started",
            "Extranonce2 size 4",
            "Subscribed to stratum",
            # ckpool sends this on every connect — benign, seen in live testing
            "Extranonce disabled, subscribe timed out",
        ):
            self.assertEqual(m.classify_line(line), "live", line)

    def test_fail_wins_over_live_keywords(self):
        # the old bug: this line contains "stratum"/"connect" and turned the UI green
        self.assertEqual(m.classify_line("Stratum connect failed"), "fail")

    def test_neutral_lines(self):
        for line in ("CPU #0: 1.62 MH/s", "Total: 3.2 MH/s", "hello"):
            self.assertIsNone(m.classify_line(line), line)


class TestExitCodes(unittest.TestCase):
    def test_known_codes_explained(self):
        self.assertIn("illegal instruction", m.explain_exit(0xC000001D))
        self.assertIn("DLL", m.explain_exit(0xC0000135))
        # subprocess on some paths reports NTSTATUS as a negative int
        self.assertIn("illegal instruction", m.explain_exit(0xC000001D - (1 << 32)))

    def test_unknown_codes_silent(self):
        for code in (None, 0, 1, 137):
            self.assertEqual(m.explain_exit(code), "")

    def test_cpu_mismatch(self):
        self.assertTrue(m.is_cpu_mismatch_exit(0xC000001D))
        self.assertTrue(m.is_cpu_mismatch_exit(0xC0000005))
        self.assertTrue(m.is_cpu_mismatch_exit(0xC000001D - (1 << 32)))
        for code in (None, 0, 1, 0xC0000135):
            self.assertFalse(m.is_cpu_mismatch_exit(code))


class TestPreferredBuilds(unittest.TestCase):
    def _with(self, **feats):
        base = {"AVX512F": False, "AVX2": False, "AVX": False, "SSE42": False,
                "AES": False, "SHA": False, "VAES": False}
        base.update(feats)
        orig = m.cpu_features
        m.cpu_features = lambda: base
        try:
            return m.preferred_builds()
        finally:
            m.cpu_features = orig

    def test_all_names_are_pinned(self):
        every = self._with(AVX512F=True, AVX2=True, AVX=True, SSE42=True,
                           AES=True, SHA=True, VAES=True)
        for name in every:
            self.assertIn(name, m.ENGINE_FILE_SHA256, name)

    def test_avx_only_cpu_gets_avx_build(self):
        # Sandy/Ivy Bridge: AVX + AES, no AVX2 — should not fall to sse42
        self.assertEqual(self._with(AVX=True, SSE42=True, AES=True)[0],
                         "cpuminer-avx.exe")

    def test_baseline_always_last(self):
        for feats in ({}, {"AVX2": True}, {"AVX512F": True, "SHA": True, "VAES": True}):
            self.assertEqual(self._with(**feats)[-1], "cpuminer-sse2.exe")

    def test_modern_ryzen_order(self):
        got = self._with(AVX2=True, SSE42=True, AES=True, SHA=True, VAES=True)
        self.assertEqual(got[0], "cpuminer-avx2-sha-vaes.exe")


class TestValidateBtcAddress(unittest.TestCase):
    VALID = [
        ("bc1q9t8v8e29xhhxlj3tt5ulj4dxal8ud3wreessha", "SegWit bc1q"),
        ("BC1Q9T8V8E29XHHXLJ3TT5ULJ4DXAL8UD3WREESSHA", "SegWit bc1q"),  # all-upper QR form
        ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "legacy P2PKH"),         # genesis block
        ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "P2SH"),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "SegWit bc1q"),  # BIP-173 vector
    ]
    INVALID = [
        ("bc1q9t8v8e29xhhxlj3tt5ulj4dxal8ud3wreesshq", "checksum"),   # in-charset flip
        ("bc1q9t8v8e29xhhxlj3tt5ulj4dxal8ud3wreesshb", "character"),  # out-of-charset char
        ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb", "checksum"),
        ("bc1Q9t8v8e29xhhxlj3tt5ulj4dxal8ud3wreessha", "mixed"),
        ("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", "mainnet"),    # testnet bech32
        ("mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn", "mainnet"),            # testnet legacy
        ("0x7C3Aa9f0aD8a9C0f9534eE55E4B4Cc3E29a3F1b0", "Ethereum"),
        ("hello", ""), ("", ""),
        ("bc1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq", ""),
    ]

    def test_valid_addresses(self):
        for a, kind in self.VALID:
            ok, detail = m.validate_btc_address(a)
            self.assertTrue(ok, (a, detail))
            self.assertIn(kind.lower(), detail.lower(), a)

    def test_invalid_addresses(self):
        for a, why in self.INVALID:
            ok, detail = m.validate_btc_address(a)
            self.assertFalse(ok, (a, detail))
            self.assertIn(why.lower(), detail.lower(), a)

    def test_whitespace_tolerated(self):
        self.assertTrue(m.validate_btc_address(
            "  bc1q9t8v8e29xhhxlj3tt5ulj4dxal8ud3wreessha \n")[0])


class TestThreadsFor(unittest.TestCase):
    def test_quarter_load_defaults(self):
        self.assertEqual(m.threads_for(25, 12), 3)
        self.assertEqual(m.threads_for(25, 8), 2)
        self.assertEqual(m.threads_for(25, 4), 1)

    def test_never_zero_threads(self):
        for ncpu in (1, 2, 3):
            self.assertEqual(m.threads_for(25, ncpu), 1)
        self.assertEqual(m.threads_for(25, None), 1)

    def test_full_load_uses_all_cores(self):
        self.assertEqual(m.threads_for(100, 12), 12)

    def test_soft_max(self):
        self.assertEqual(m.threads_for(80, 12), 10)

    def test_clamps_and_garbage(self):
        self.assertEqual(m.threads_for(5, 12), m.threads_for(m.CPU_PCT_MIN, 12))
        self.assertEqual(m.threads_for(250, 12), 12)
        self.assertEqual(m.threads_for("junk", 12), m.threads_for(m.CPU_PCT_DEFAULT, 12))


class TestMinetestGuard(unittest.TestCase):
    def test_rejects_missing_or_bad_address(self):
        tmp = tempfile.mkdtemp(prefix="slm-mt-")
        orig = m.app_dir
        m.app_dir = lambda: tmp
        try:
            for bad in ("", "not-an-address", None):
                m._minetest(1, bad, 1)
                out = open(os.path.join(tmp, "sololuck_minetest.txt")).read()
                self.assertIn("RESULT: FAIL", out)
                self.assertIn("bad or missing BTC address", out)
        finally:
            m.app_dir = orig


class TestIcon(unittest.TestCase):
    def test_icon_is_valid_png(self):
        import base64
        raw = base64.b64decode(m.ICON_B64, validate=True)
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")


class TestCpuSpec(unittest.TestCase):
    def test_tier_of(self):
        self.assertEqual(m.tier_of("cpuminer-avx512-sha-vaes.exe"), "AVX-512 + SHA")
        self.assertEqual(m.tier_of("cpuminer-avx2-sha-vaes.exe"), "AVX2 + SHA")
        self.assertEqual(m.tier_of("cpuminer-aes-sse42.exe"), "SSE4.2 + AES")
        self.assertEqual(m.tier_of("cpuminer-sse2.exe"), "SSE2 (baseline)")
        self.assertEqual(m.tier_of(""), "")

    def test_cpu_spec_shape(self):
        s = m.cpu_spec()
        self.assertIn("brand", s)
        self.assertGreaterEqual(s["logical"], 1)
        self.assertIn("tier", s)
        self.assertTrue(s["brand"])

    def test_meter_graceful_offwindows(self):
        # off-Windows sample() must never raise and returns None
        self.assertIsNone(m.CpuMeter().sample())


class TestVersionCompare(unittest.TestCase):
    def test_ordering(self):
        self.assertGreater(m._version_tuple("1.5.0"), m._version_tuple("1.4.0"))
        self.assertGreater(m._version_tuple("1.10.0"), m._version_tuple("1.9.0"))
        self.assertGreater(m._version_tuple("2.0.0"), m._version_tuple("1.99.99"))
        self.assertEqual(m._version_tuple("1.5.0"), m._version_tuple("1.5.0"))

    def test_garbage_versions_safe(self):
        self.assertEqual(m._version_tuple("v1.5.0-beta"), (1, 5, 0))
        self.assertEqual(m._version_tuple(""), (0,))


class TestCheckForUpdate(unittest.TestCase):
    def setUp(self):
        self._orig = m._http_get

    def tearDown(self):
        m._http_get = self._orig

    def _serve(self, obj):
        m._http_get = lambda url, want_json=False, timeout=15: obj

    def test_newer_offered(self):
        self._serve({"version": "9.9.9", "file": "SoloLuckMiner-v9.9.9.exe", "sha256": "a" * 64})
        info = m.check_for_update("1.5.0")
        self.assertEqual(info["version"], "9.9.9")
        self.assertTrue(info["url"].endswith("SoloLuckMiner-v9.9.9.exe"))

    def test_same_or_older_declined(self):
        self._serve({"version": "1.5.0", "file": "x.exe", "sha256": "a" * 64})
        self.assertIsNone(m.check_for_update("1.5.0"))
        self._serve({"version": "1.0.0", "file": "x.exe", "sha256": "a" * 64})
        self.assertIsNone(m.check_for_update("1.5.0"))

    def test_bad_sha_declined(self):
        self._serve({"version": "9.9.9", "file": "x.exe", "sha256": "NOPE"})
        self.assertIsNone(m.check_for_update("1.5.0"))

    def test_missing_fields_declined(self):
        self._serve({"version": "9.9.9"})
        self.assertIsNone(m.check_for_update("1.5.0"))

    def test_offline_declined(self):
        def boom(*a, **k):
            raise OSError("offline")
        m._http_get = boom
        self.assertIsNone(m.check_for_update("1.5.0"))


class TestDownloadUpdate(unittest.TestCase):
    def setUp(self):
        self._orig_http = m._http_get
        self._orig_dir = m._update_dir
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="slupd-")
        m._update_dir = lambda: self.tmp

    def tearDown(self):
        m._http_get = self._orig_http
        m._update_dir = self._orig_dir
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_verified_download_saved(self):
        import hashlib
        blob = b"MZ new-version-exe"
        m._http_get = lambda url, timeout=300: blob
        info = {"file": "SoloLuckMiner-v9.9.9.exe", "url": "https://x/f.exe",
                "sha256": hashlib.sha256(blob).hexdigest()}
        path = m.download_update(info)
        self.assertTrue(path.endswith("SoloLuckMiner-v9.9.9.exe"))
        self.assertEqual(open(path, "rb").read(), blob)

    def test_tampered_download_fails_closed(self):
        import hashlib, os as _os
        m._http_get = lambda url, timeout=300: b"TAMPERED"
        info = {"file": "u.exe", "url": "https://x/u.exe",
                "sha256": hashlib.sha256(b"original").hexdigest()}
        with self.assertRaisesRegex(RuntimeError, "SECURITY"):
            m.download_update(info)
        self.assertFalse(_os.path.exists(_os.path.join(self.tmp, "u.exe")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
