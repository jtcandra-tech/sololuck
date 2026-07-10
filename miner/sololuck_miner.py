#!/usr/bin/env python3
"""
SoloLuck Miner — a simple Start/Stop GUI that drives the cpuminer-opt CPU engine
and points it at the SoloLuck solo Bitcoin pool (sololuck.io).

A PC's hashrate is tiny next to an ASIC, so this is a long shot — which is
exactly the point of a solo pool. If your CPU happens to solve a block, the whole
reward is paid straight to your address on-chain (0% pool fee, finders keepers).

This is a CLEAN WRAPPER: it contains no mining code itself. On first run it detects
your CPU and downloads the matching build from the PINNED cpuminer-opt release
(Jay D Dee's, GPLv2), verifies its SHA-256 against the manifest baked into this
file, and only then runs it. An engine that fails verification is quarantined
and never executed. Nothing to install. (Advanced
users can drop their own cpuminer-opt.exe next to this app to skip the download.)

Why a clean wrapper: a GUI with no embedded miner isn't itself flagged as a coin
miner, so the app always launches; antivirus only ever flags the downloaded engine,
which you whitelist once. Pure Python standard library (tkinter + urllib).
"""
import base64
import hashlib
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile
try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # headless (tests) — GUI not needed for the engine logic
    tk = messagebox = ttk = None

APP_NAME = "SoloLuck Miner"
DEFAULT_HOST = "sololuck.io"
DEFAULT_PORT = "3335"   # Nano tier — diff 1, tuned for CPUs so shares register fast
ALGO = "sha256d"   # Bitcoin
ENGINE_DIR_NAME = "SoloLuckMiner-engine"
APP_VERSION = "1.10.0"
CHANGELOG_URL = "https://sololuck.io/changelog"
# The pool endpoint is fixed: this is the SoloLuck app, and the Nano tier's
# difficulty is what makes CPU shares register fast. (Generic pools have
# generic miners; this one has one job.)
POOL_LABEL = "stratum+tcp://%s:%s" % (DEFAULT_HOST, DEFAULT_PORT)
# CPU load slider: gentle by default. Full load makes a PC noticeably slower,
# so 100% is opt-in via an explicit checkbox; without it the slider tops out
# at the soft max.
CPU_PCT_MIN = 25
CPU_PCT_SOFT_MAX = 80      # green "recommended" ceiling; above this is amber "high load"
CPU_PCT_HARD_MAX = 90      # v1.8: absolute cap — the miner never uses more, so the PC
                          # stays usable and we shed only the top threads (little hashrate,
                          # lots of heat). There is no 100% option any more.
CPU_PCT_DEFAULT = 25
# ── pinned mining engine ──────────────────────────────────────────────────────
# Exact release, exact bytes. The app never fetches "latest", never falls back
# to another URL or version, and never executes an engine file whose SHA-256
# does not match this manifest (fail closed). Hashes computed from the official
# release asset at pin time.
ENGINE_VERSION = "v26.1"
ENGINE_ZIP_URL = ("https://github.com/JayDDee/cpuminer-opt/releases/download/"
                  "v26.1/cpuminer-opt-26.1-windows.zip")
ENGINE_ZIP_SHA256 = "caf59deb12831e40475c5245a76bf42f9ba2ff620065be5386b80ec55c998e9c"
ENGINE_FILE_SHA256 = {
    "cpuminer-aes-sse42.exe": "454f52e4d9074a089fe2c0daefb5635ad5eae32a4bcb94b878190ae8843db547",
    "cpuminer-avx2.exe": "4823226ef2031ad356d6a01d75d3dbce0aeb57054e2ef9d0e9bfc96e68bd42c7",
    "cpuminer-avx2-sha.exe": "d4d1b9b66060e9453f597f54c3ee5704b82239bf94ff445a5f0f5de78af94519",
    "cpuminer-avx2-sha-vaes.exe": "5cbae7a39b6ea0f3ca400523c300880a828b40a8470e1194890722807af9d780",
    "cpuminer-avx512.exe": "453c351dfd0af95e497346fe2f2b8d7b3dbc90757169f09679d7b6fe8b0958ec",
    "cpuminer-avx512-sha-vaes.exe": "a41c835bff8c404f0dc79a4f86a666f1e44dac9e5758bc31845b9dd7072f2b17",
    "cpuminer-avx.exe": "adda67c2db1398c90adfd60498425df81b59ae0cd4924ed8d4a5c7d13d731005",
    "cpuminer-sse2.exe": "f32e00a6947113c7da8a83940172b6f4519fa658fd0d2839ce69a00464f3798a",
    "libcurl-4.dll": "218cfc4073bab4eddf0de0804f96b204687311e20a9e97994bff54c9b0e01ee9",
    "libgcc_s_seh-1.dll": "c82f84171b9246d1cac261100b2199789c96c37b03b375f33b2c72afab060b05",
    "libstdc++-6.dll": "baef1f4cabebdadc52213761b4c8e2bf381976a67bd7c490f952c38f6831b036",
    "libwinpthread-1.dll": "2f9984c591a5654434c53e8b4d0c5c187f1fd0bab95247d5c9bc1c0bd60e6232",
    "zlib1.dll": "61b71a00bf87ea1a63a66677de5208db7e4407287ff668e526ad609a70ef3f12",
}
# cpuminer-opt isn't statically linked — these ride next to whichever build we use.
ENGINE_DLLS = ["libcurl-4.dll", "libgcc_s_seh-1.dll", "libstdc++-6.dll",
               "libwinpthread-1.dll", "zlib1.dll"]
# user-supplied override builds we recognise in the app folder.
MINER_NAMES = [
    "cpuminer-opt.exe", "cpuminer.exe",
    "cpuminer-avx2.exe", "cpuminer-avx512.exe", "cpuminer-avx.exe",
    "cpuminer-zen.exe", "cpuminer-zen3.exe", "cpuminer-zen4.exe", "cpuminer-zen5.exe",
    "cpuminer-sse2.exe", "cpuminer-sse42.exe", "cpuminer-aes-sse42.exe",
    "cpuminer-opt", "cpuminer",   # non-Windows dev fallbacks
]
HASH_RE = re.compile(r"([\d.]+)\s*([kKMGTP]?)[hH]/s")
ACCEPT_RE = re.compile(r"[Aa]ccepted\s+(\d+)/(\d+)")
# connection-state classification of cpuminer output lines (order matters:
# a failure line often also contains the word "stratum"/"connect")
_FAIL_RE = re.compile(r"connection (failed|interrupted|timed? ?out|refused|reset|closed|lost)"
                      r"|failed to connect|unable to connect|connect failed"
                      # bare timeouts count, but ckpool's benign 'Extranonce disabled,
                      # subscribe timed out' on every connect does not
                      r"|(?<!subscribe )timed? ?out"
                      r"|retry (in|after)|retrying"
                      r"|stratum authentication failed|authorization failed|login failed", re.I)
_LIVE_RE = re.compile(r"difficulty (set|changed)|stratum diff|new (work|job|block)"
                      r"|threads? started|extranonce|authoriz|subscrib"
                      r"|connection established|connected to", re.I)


def classify_line(line):
    """'fail' (connection/auth problem), 'live' (talking to the pool), or None."""
    if _FAIL_RE.search(line):
        return "fail"
    if _LIVE_RE.search(line):
        return "live"
    return None


# Windows NTSTATUS exit codes worth translating for the user.
_EXIT_HELP = {
    0xC000001D: "illegal instruction — this engine build needs CPU features this machine doesn't have",
    0xC0000005: "access violation — the engine crashed",
    0xC0000135: "a required DLL is missing from the engine folder",
    0xC0000409: "the engine crashed (stack error)",
}


def explain_exit(code):
    """Human hint for an engine exit code ('' when there is nothing to add)."""
    if code is None:
        return ""
    msg = _EXIT_HELP.get(code & 0xFFFFFFFF)
    return (" — " + msg) if msg else ""


def is_cpu_mismatch_exit(code):
    """True for the crash signatures a too-new build throws on an older CPU."""
    return code is not None and (code & 0xFFFFFFFF) in (0xC000001D, 0xC0000005)


def threads_for(pct, ncpu):
    """Miner thread count for a CPU-load percentage. Always at least 1."""
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        pct = CPU_PCT_DEFAULT
    pct = max(CPU_PCT_MIN, min(CPU_PCT_HARD_MAX, pct))
    return max(1, int(round((ncpu or 1) * pct / 100.0)))


# ── Bitcoin address validation (real checksums, not just shape) ───────────────
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values):
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= gen[i]
    return chk


def validate_btc_address(addr):
    """(ok, detail): base58check for 1…/3…, BIP-173/BIP-350 bech32(m) for bc1….
    detail = the address kind when valid, else why it failed. Mainnet only —
    a mistyped payout address on a solo pool means an unclaimable block."""
    a = (addr or "").strip()
    if not a:
        return (False, "empty")
    low = a.lower()
    if low.startswith(("tb1", "bcrt1")) or a[0] in "mn2" or a[:2] == "0x":
        return (False, "not a mainnet Bitcoin address" if a[:2] != "0x"
                else "that looks like an Ethereum address")
    if len(a) < 14:
        return (False, "too short")
    if a[0] in "13":
        n = 0
        for ch in a:
            i = _B58_ALPHABET.find(ch)
            if i < 0:
                return (False, "invalid character '%s'" % ch)
            n = n * 58 + i
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        raw = b"\x00" * (len(a) - len(a.lstrip("1"))) + raw
        if len(raw) != 25:
            return (False, "wrong length")
        if hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] != raw[21:]:
            return (False, "checksum failed — typo?")
        return (True, "legacy P2PKH" if raw[0] == 0x00 else "P2SH")
    if low.startswith("bc1"):
        if a != low and a != a.upper():
            return (False, "mixed upper/lower case")
        sep = low.rfind("1")
        if low[:sep] != "bc":
            return (False, "unrecognized format")
        vals = []
        for ch in low[sep + 1:]:
            i = _B32_CHARSET.find(ch)
            if i < 0:
                return (False, "invalid character '%s'" % ch)
            vals.append(i)
        if len(vals) < 7:
            return (False, "too short")
        hrpexp = [ord(c) >> 5 for c in "bc"] + [0] + [ord(c) & 31 for c in "bc"]
        witver = vals[0]
        if witver > 16:
            return (False, "bad witness version")
        want = 1 if witver == 0 else 0x2BC830A3   # bech32 v0, bech32m v1+
        if _bech32_polymod(hrpexp + vals) != want:
            return (False, "checksum failed — typo?")
        acc = bits = 0
        prog = []
        for v in vals[1:-6]:
            acc = (acc << 5) | v
            bits += 5
            while bits >= 8:
                bits -= 8
                prog.append((acc >> bits) & 0xFF)
        if bits >= 5 or (acc & ((1 << bits) - 1)):
            return (False, "invalid padding")
        n = len(prog)
        if witver == 0:
            if n == 20:
                return (True, "SegWit bc1q")
            if n == 32:
                return (True, "SegWit bc1q (script)")
            return (False, "wrong program length")
        if not 2 <= n <= 40:
            return (False, "wrong program length")
        if witver == 1 and n == 32:
            return (True, "Taproot bc1p")
        return (True, "SegWit v%d" % witver)
    return (False, "unrecognized format")

# colours (brand-ish, works on the default tk theme)
BG = "#0b0e14"
CARD = "#11161f"
FG = "#dfe6f0"
MUTED = "#9fb0c5"
ORANGE = "#f7931a"
ORANGE_HOT = "#ffa733"
GREEN = "#3ad17a"
RED = "#ff6b6b"
BORDER = "#1c2534"

# 32×32 window icon (orange rounded square + bolt), PNG, generated at build time
ICON_B64 = ("iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAABCklEQVR42tWXsQ3CMBBF0yNRESBI"
            "lHRMQEPNCizAArABG7AEezAHc1Aa/UgnWVbi+O47JkT6RaQo/8n/7DtX1dSfz2PjGP3ElIIZyzwJ"
            "YmzzKAT70+dl3coMwZi/743bbWtuFRiA475uZQZgzK+nlZvNF+58WNprgckd5pAm/ywAkrsA4L0o"
            "ADIXc4jajtbcRZb8zQB+7iJEAYhQr1uTFyDMPaaUVVEDhLn3CZApRakCCHOPKXVHJAN05d4nUz8Y"
            "+hDFJM1G1BUHVqnYOYAi8821vYAGsBRdNgCY+QBD+z07ANuEaADJ33oE0zMBMtdOQFknImvRZQHQ"
            "Dp9FJ+O/uBtM/2o2ictpyecL3pHtrp/wV0YAAAAASUVORK5CYII=")


def app_dir():
    """Folder the app lives in — works for both `python script.py` and a
    PyInstaller --onefile .exe."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CFG_PATH = os.path.join(app_dir(), "sololuck_miner.cfg")


# ── CPU feature detection (decides which cpuminer-opt build to fetch) ──────────
_PF = {"SSE42": 38, "AVX": 39, "AVX2": 40, "AVX512F": 41}


def _has(feat):
    """True if the running CPU/OS reports the given instruction-set feature."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(_PF[feat]))
    except Exception:
        return False


def _cpuid(leaf, subleaf=0):
    """x86-64 CPUID via a tiny ctypes shellcode stub → (eax,ebx,ecx,edx). Win64 only."""
    if os.name != "nt":
        raise OSError("cpuid: Windows only")
    import ctypes
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise OSError("cpuid: 64-bit only")
    code = bytes((
        0x53, 0x4D, 0x89, 0xC1, 0x89, 0xC8, 0x89, 0xD1, 0x0F, 0xA2,
        0x41, 0x89, 0x01, 0x41, 0x89, 0x59, 0x04, 0x41, 0x89, 0x49, 0x08,
        0x41, 0x89, 0x51, 0x0C, 0x5B, 0xC3,
    ))
    k = ctypes.windll.kernel32
    k.VirtualAlloc.restype = ctypes.c_void_p
    k.VirtualAlloc.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong, ctypes.c_ulong)
    addr = k.VirtualAlloc(None, len(code), 0x3000, 0x40)
    if not addr:
        raise OSError("cpuid: VirtualAlloc failed")
    try:
        ctypes.memmove(addr, code, len(code))
        out = (ctypes.c_uint32 * 4)()
        proto = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
        proto(addr)(leaf, subleaf, ctypes.cast(out, ctypes.c_void_p))
        return (out[0], out[1], out[2], out[3])
    finally:
        k.VirtualFree.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong)
        k.VirtualFree(addr, 0, 0x8000)


def cpu_features():
    """AVX-family from the OS; AES/SHA-NI/VAES from CPUID. Fails safe to False."""
    f = {"AVX512F": _has("AVX512F"), "AVX2": _has("AVX2"),
         "AVX": _has("AVX"), "SSE42": _has("SSE42"),
         "AES": False, "SHA": False, "VAES": False}
    try:
        _, _, ecx1, _ = _cpuid(1)
        _, ebx7, ecx7, _ = _cpuid(7, 0)
        f["AES"] = bool(ecx1 & (1 << 25))
        f["SHA"] = bool(ebx7 & (1 << 29))
        f["VAES"] = bool(ecx7 & (1 << 9))
        if not f["SSE42"]:
            f["SSE42"] = bool(ecx1 & (1 << 20))
    except Exception:
        pass
    return f


def cpu_signature():
    import platform
    fe = cpu_features()
    flags = "".join("1" if fe[k] else "0" for k in
                    ("AVX512F", "AVX2", "AVX", "SSE42", "AES", "SHA", "VAES"))
    try:
        brand = platform.processor() or ""
    except Exception:
        brand = ""
    return flags + "|" + brand


def preferred_builds():
    """cpuminer-opt build names best→safest for THIS CPU; ends at the universal sse2."""
    f = cpu_features()
    c = []
    if f["AVX512F"]:
        if f["SHA"] and f["VAES"]:
            c.append("cpuminer-avx512-sha-vaes.exe")
        c.append("cpuminer-avx512.exe")
    if f["AVX2"]:
        if f["SHA"] and f["VAES"]:
            c.append("cpuminer-avx2-sha-vaes.exe")
        if f["SHA"]:
            c.append("cpuminer-avx2-sha.exe")
        c.append("cpuminer-avx2.exe")
    if f["AVX"]:
        c.append("cpuminer-avx.exe")
    if f["SSE42"] and f["AES"]:
        c.append("cpuminer-aes-sse42.exe")
    c.append("cpuminer-sse2.exe")
    seen, out = set(), []
    for x in c:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ── CPU identity (for the spec readout) ───────────────────────────────────────
def tier_of(build_name):
    """Human name for the SIMD path a cpuminer build uses (drives per-core speed)."""
    n = (build_name or "").lower()
    if "avx512-sha" in n:
        return "AVX-512 + SHA"
    if "avx512" in n:
        return "AVX-512"
    if "avx2-sha" in n:
        return "AVX2 + SHA"
    if "avx2" in n:
        return "AVX2"
    if "avx" in n:
        return "AVX"
    if "aes-sse42" in n or "sse42" in n:
        return "SSE4.2 + AES"
    if "sse2" in n:
        return "SSE2 (baseline)"
    return ""


def cpu_brand():
    """Marketing name of the CPU (e.g. 'AMD Ryzen 7 9800X3D'). CPUID brand string
    on Windows; falls back to platform/env. Never raises."""
    if os.name == "nt":
        try:
            if _cpuid(0x80000000)[0] >= 0x80000004:
                buf = b""
                for leaf in (0x80000002, 0x80000003, 0x80000004):
                    for reg in _cpuid(leaf):
                        buf += int(reg).to_bytes(4, "little")
                s = buf.split(b"\x00")[0].decode("ascii", "replace").strip()
                if s:
                    return " ".join(s.split())  # collapse the padding spaces
        except Exception:
            pass
    try:
        import platform
        return platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "CPU")
    except Exception:
        return "CPU"


def physical_cores():
    """Physical core count via GetLogicalProcessorInformation (Win64). None if
    unknown — the caller shows logical threads instead."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        class _SLPI(ctypes.Structure):
            _fields_ = [("mask", ctypes.c_size_t),
                        ("relationship", ctypes.c_uint32),
                        ("_pad", ctypes.c_ubyte * 20)]
        k = ctypes.windll.kernel32
        rl = ctypes.c_uint32(0)
        k.GetLogicalProcessorInformation(None, ctypes.byref(rl))  # sizing call
        n = rl.value // ctypes.sizeof(_SLPI)
        if n <= 0:
            return None
        arr = (_SLPI * n)()
        if not k.GetLogicalProcessorInformation(arr, ctypes.byref(rl)):
            return None
        cores = sum(1 for x in arr if x.relationship == 0)  # RelationProcessorCore
        return cores or None
    except Exception:
        return None


def cpu_spec():
    """{brand, physical, logical, tier, build} — everything the spec line shows."""
    logical = os.cpu_count() or 1
    builds = preferred_builds()
    build = builds[0] if builds else ""
    return {"brand": cpu_brand(), "physical": physical_cores(),
            "logical": logical, "tier": tier_of(build), "build": build}


class CpuMeter:
    """Per-logical-core busy% from NtQuerySystemInformation (Win64, no deps).
    sample() returns a list of 0-100 ints (one per logical CPU) or None."""
    _SPPI = 8  # SystemProcessorPerformanceInformation

    def __init__(self):
        self.n = os.cpu_count() or 1
        self._prev = None
        self._ok = os.name == "nt"
        if self._ok:
            try:
                import ctypes

                class _PI(ctypes.Structure):
                    _fields_ = [("Idle", ctypes.c_int64), ("Kernel", ctypes.c_int64),
                                ("User", ctypes.c_int64), ("Dpc", ctypes.c_int64),
                                ("Int", ctypes.c_int64), ("IntCount", ctypes.c_uint32)]
                self._PI = _PI
                self._ntdll = ctypes.windll.ntdll
                self._ctypes = ctypes
            except Exception:
                self._ok = False

    def _read(self):
        c = self._ctypes
        arr = (self._PI * self.n)()
        ret = c.c_uint32(0)
        st = self._ntdll.NtQuerySystemInformation(self._SPPI, arr, c.sizeof(arr), c.byref(ret))
        if st != 0:
            return None
        return [(p.Idle, p.Kernel, p.User) for p in arr]

    def sample(self):
        if not self._ok:
            return None
        try:
            cur = self._read()
        except Exception:
            self._ok = False
            return None
        if not cur:
            return None
        out, prev = None, self._prev
        if prev and len(prev) == len(cur):
            out = []
            for (i0, k0, u0), (i1, k1, u1) in zip(prev, cur):
                total = (k1 - k0) + (u1 - u0)   # KernelTime includes idle
                idle = i1 - i0
                busy = 0 if total <= 0 else max(0.0, min(1.0, (total - idle) / total))
                out.append(int(round(busy * 100)))
        self._prev = cur
        return out


# ── auto-update (check sololuck.io, verify the new exe's SHA-256, relaunch) ────
LATEST_URL = "https://sololuck.io/miner-latest.json"


def _version_tuple(s):
    out = []
    for part in str(s).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def check_for_update(current=APP_VERSION):
    """Ask the site for the latest version. Returns the release dict
    {version,file,url,sha256} only if it is strictly newer than `current`,
    else None. Never raises (offline / blocked → None)."""
    try:
        info = _http_get(LATEST_URL, want_json=True, timeout=15)
    except Exception:
        return None
    if not isinstance(info, dict) or "version" not in info:
        return None
    if _version_tuple(info["version"]) <= _version_tuple(current):
        return None
    url = info.get("url") or ("https://sololuck.io/" + info.get("file", ""))
    sha = (info.get("sha256") or "").lower()
    if not (info.get("file") and re.fullmatch(r"[0-9a-f]{64}", sha)):
        return None
    info["url"] = url
    return info


def _update_dir():
    """A writable folder to drop the new exe in (prefer next to the current app)."""
    for base in (app_dir(), os.environ.get("USERPROFILE", ""), os.environ.get("TEMP", "")):
        if not base:
            continue
        try:
            t = os.path.join(base, ".sl_wtest")
            with open(t, "w"):
                pass
            os.remove(t)
            return base
        except Exception:
            continue
    return app_dir()


def download_update(info, report=lambda s: None):
    """Fetch the newer versioned exe, verify its SHA-256 against the manifest
    (fail closed), and return the local path. No overwrite of the running exe —
    the file is versioned, so the new one just sits beside the old."""
    dest = os.path.join(_update_dir(), info["file"])
    report("Downloading %s…" % info["file"])
    blob = _http_get(info["url"], timeout=300)
    got = hashlib.sha256(blob).hexdigest()
    if got != info["sha256"].lower():
        raise RuntimeError("SECURITY: the downloaded update does not match the "
                           "published SHA-256.\nExpected %s\nGot      %s\nNothing was saved."
                           % (info["sha256"], got))
    with open(dest, "wb") as f:
        f.write(blob)
    report("Update verified — SHA-256 OK.")
    return dest


# ── engine location (a stable, antivirus-excludable folder next to the app) ───
def engine_dir():
    """A stable, writable folder to keep the downloaded engine in (so the path
    doesn't change and the user can add ONE antivirus exclusion that sticks)."""
    for base in (app_dir(), os.environ.get("LOCALAPPDATA", ""), os.environ.get("TEMP", "")):
        if not base:
            continue
        d = os.path.join(base, ENGINE_DIR_NAME)
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, ".wtest")
            with open(t, "w"):
                pass
            os.remove(t)
            return d
        except Exception:
            continue
    return os.path.join(app_dir(), ENGINE_DIR_NAME)


def _ps_run(command, timeout=15):
    """Run a short PowerShell command hidden, return stdout (Windows only)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    out = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                         capture_output=True, text=True, timeout=timeout,
                         creationflags=flags)
    return out.stdout or ""


def av_exclusion_present():
    """Is the engine folder already a Windows Defender path-exclusion?
    True / False on Windows; None when we can't tell (not Windows, no Defender,
    query blocked) — callers hide the UI on None rather than nag."""
    if os.name != "nt":
        return None
    try:
        d = os.path.normcase(os.path.normpath(engine_dir()))
        paths = [os.path.normcase(os.path.normpath(p.strip()))
                 for p in _ps_run("(Get-MpPreference).ExclusionPath").splitlines() if p.strip()]
        return d in paths
    except Exception:
        return None


def add_av_exclusion():
    """Add a Windows Defender exclusion for the engine folder so the mining engine
    is never quarantined — REAL-TIME PROTECTION STAYS ON for everything else; only
    this one folder is excluded. Exclusions are machine-wide and need admin, so it
    elevates via a single UAC prompt and WAITS for it to finish (synchronous, so a
    caller can safely download into the folder right after). Also restores anything
    already quarantined from the folder. Returns True only when the exclusion is
    confirmed in place; False if declined / failed / non-Windows.

    Call this OFF the UI thread — it blocks on the elevation prompt."""
    if os.name != "nt":
        return False
    try:
        d = engine_dir().replace("'", "''")
        # elevated child: add the path + process exclusions, then un-quarantine any
        # engine Defender already took from the folder (best-effort).
        inner = ("Add-MpPreference -ExclusionPath '%s'; "
                 "Add-MpPreference -ExclusionProcess 'cpuminer-*.exe'; "
                 "$m=Join-Path $env:ProgramFiles 'Windows Defender\\MpCmdRun.exe'; "
                 "if(Test-Path $m){ & $m -Restore -Path '%s' 2>$null }" % (d, d))
        b64 = base64.b64encode(inner.encode("utf-16-le")).decode()
        # non-elevated launcher raises the ONE UAC prompt and waits for the child
        outer = ("try{ Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
                 "-ArgumentList @('-NoProfile','-EncodedCommand','%s'); exit 0 }"
                 "catch{ exit 1 }" % b64)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        r = subprocess.run(["powershell", "-NoProfile", "-Command", outer],
                           timeout=180, creationflags=flags)
        return r.returncode == 0 and av_exclusion_present() is True
    except Exception:
        return False


def _shield_engine_folder(report=lambda s: None):
    """Exclude the engine folder from Defender BEFORE the engine is written to disk,
    so a fresh download is never quarantined. Real-time protection stays fully ON —
    this only tells Defender to skip one folder. Best-effort: if it's already
    excluded we do nothing, and if the user declines the prompt we still try the
    download (the engine may then be blocked, and the UI offers 'Shield it' + retry)."""
    if os.name != "nt":
        return
    try:
        if av_exclusion_present():
            return
        report("Adding a Windows Security exclusion for the engine folder — approve "
               "the prompt. Real-time protection stays ON; only this folder is skipped…")
        if add_av_exclusion():
            report("Engine folder shielded — the fast engine won't be quarantined.")
        else:
            report("Not shielded (you can click ‘Shield it’ later). Continuing…")
    except Exception:
        pass


def find_user_miner():
    """A user-supplied cpuminer build dropped in the app folder (skips the download)."""
    d = app_dir()
    for name in MINER_NAMES:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    try:
        for f in os.listdir(d):
            low = f.lower()
            if low == ENGINE_DIR_NAME.lower():
                continue
            if low.startswith("cpuminer") and (low.endswith(".exe") or "." not in low):
                return os.path.join(d, f)
    except OSError:
        pass
    return None


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_log(msg):
    """Verification audit trail: engine-verify.log next to the engine + stdout."""
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    try:
        with open(os.path.join(engine_dir(), "engine-verify.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def verify_engine_file(path):
    """True iff the file's bytes match the pinned manifest for its filename."""
    name = os.path.basename(path)
    want = ENGINE_FILE_SHA256.get(name)
    if not want:
        _verify_log("verify %s: not in the pinned %s manifest -> UNVERIFIED" % (name, ENGINE_VERSION))
        return False
    try:
        got = _sha256_file(path)
    except OSError as e:
        _verify_log("verify %s: unreadable (%s) -> FAIL" % (name, e))
        return False
    ok = got == want
    _verify_log("verify %s: expected=%s actual=%s -> %s" % (name, want, got, "OK" if ok else "MISMATCH"))
    return ok


def _quarantine(path):
    """Never execute a bad engine — move it aside (or delete if rename fails)."""
    try:
        os.replace(path, path + ".quarantined")
        _verify_log("quarantined %s" % path)
    except OSError:
        try:
            os.remove(path)
            _verify_log("deleted unverifiable %s" % path)
        except OSError:
            pass


def find_local_engine():
    """The best already-downloaded build in the engine folder, SHA-256-verified
    against the pinned manifest. A cached file is never assumed trusted; a
    mismatching one is quarantined and never returned."""
    d = engine_dir()
    for b in preferred_builds():
        p = os.path.join(d, b)
        if os.path.isfile(p):
            if verify_engine_file(p):
                return p
            _quarantine(p)
    return None


def _http_get(url, want_json=False, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "SoloLuckMiner",
                                               "Accept": "application/octet-stream" if not want_json
                                               else "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data.decode("utf-8")) if want_json else data


def download_engine(report=lambda s: None):
    """Fetch the PINNED cpuminer-opt release archive, verify its SHA-256, then
    extract the build matching this CPU (+ sse2 fallback + runtime DLLs), each
    re-verified on disk. Fails closed: any mismatch aborts with a security
    error and nothing unverified is left behind. No mirrors, no 'latest'."""
    dest = engine_dir()
    # Shield the folder FIRST so Windows Defender can't quarantine the engine as it
    # lands — real-time protection stays on; only this folder is excluded.
    _shield_engine_folder(report)
    report("Downloading the pinned mining engine cpuminer-opt %s (~18 MB, one time)…" % ENGINE_VERSION)
    _verify_log("download url=%s engine=%s" % (ENGINE_ZIP_URL, ENGINE_VERSION))
    blob = _http_get(ENGINE_ZIP_URL, timeout=300)
    got = hashlib.sha256(blob).hexdigest()
    _verify_log("archive expected=%s actual=%s -> %s"
                % (ENGINE_ZIP_SHA256, got, "OK" if got == ENGINE_ZIP_SHA256 else "MISMATCH"))
    if got != ENGINE_ZIP_SHA256:
        raise RuntimeError(
            "SECURITY: the downloaded engine archive does not match the pinned "
            "SHA-256 for cpuminer-opt %s.\nExpected %s\nGot      %s\n"
            "Nothing was installed. Check your connection (proxy or antivirus "
            "interception can cause this) and try again." % (ENGINE_VERSION, ENGINE_ZIP_SHA256, got))
    report("Archive verified · unpacking…")
    zf = zipfile.ZipFile(io.BytesIO(blob))
    members = {os.path.basename(n): n for n in zf.namelist() if not n.endswith("/")}
    wanted = []
    for b in preferred_builds():
        if b in members and b in ENGINE_FILE_SHA256:
            wanted.append(b)
            break
    if ("cpuminer-sse2.exe" in members and "cpuminer-sse2.exe" in ENGINE_FILE_SHA256
            and "cpuminer-sse2.exe" not in wanted):
        wanted.append("cpuminer-sse2.exe")
    if not any(w.endswith(".exe") for w in wanted):
        raise RuntimeError("Unsupported CPU: no matching cpuminer-opt %s build for this machine."
                           % ENGINE_VERSION)
    for d in ENGINE_DLLS:
        if d in members:
            wanted.append(d)
    os.makedirs(dest, exist_ok=True)
    for name in wanted:
        p = os.path.join(dest, name)
        with zf.open(members[name]) as srcf, open(p, "wb") as out:
            out.write(srcf.read())
        if not verify_engine_file(p):
            _quarantine(p)
            raise RuntimeError("SECURITY: %s failed verification after extraction; "
                               "it was quarantined and will not run." % name)
    eng = find_local_engine()
    if not eng:
        raise RuntimeError("Engine unpacked but no runnable verified build was produced.")
    report("Engine cpuminer-opt %s installed — SHA-256 verified." % ENGINE_VERSION)
    return eng


def _confirm_unverified_user_engine(path):
    """A user-supplied engine that is NOT the pinned build runs only after an
    explicit, informed yes. Headless: refuse (fail closed)."""
    if messagebox is None:
        return False
    try:
        return bool(messagebox.askyesno(
            APP_NAME,
            "You placed your own engine next to the app:\n%s\n\n"
            "It is NOT the SHA-256-verified cpuminer-opt %s build this app pins, so "
            "SoloLuck cannot vouch for it.\n\nRun YOUR file anyway?" % (path, ENGINE_VERSION)))
    except Exception:  # no display / dialog failure — fail closed
        return False


def ensure_engine(report=lambda s: None, confirm_unverified=None):
    """Resolve a runnable engine: user override (verified, or explicitly
    user-confirmed) → verified cached copy → verified pinned download."""
    up = find_user_miner()
    if up:
        if verify_engine_file(up):
            report("Engine cpuminer-opt %s (your copy) — SHA-256 verified." % ENGINE_VERSION)
            return up
        ok = (confirm_unverified or _confirm_unverified_user_engine)(up)
        _verify_log("user-supplied engine %s unverified -> %s"
                    % (up, "user accepted" if ok else "refused"))
        if ok:
            return up
    eng = find_local_engine()
    if eng:
        report("Engine cpuminer-opt %s — SHA-256 verified." % ENGINE_VERSION)
        return eng
    return download_engine(report)


def _engine_missing_msg():
    """Actionable message when the engine isn't available — download failure or, most
    often, antivirus blocking the downloaded cpuminer engine."""
    folder = engine_dir()
    return ("The mining engine isn't available.\n\n"
            "Either the one-time download didn't finish (check your internet), or — most "
            "likely — your antivirus / Windows Defender blocked or removed it. EVERY CPU "
            "miner trips this false-positive; it's the cpuminer-opt engine, not this app, "
            "and it does nothing but hash.\n\n"
            "Fix it once:\n"
            "  1. Windows Security  →  Virus & threat protection.\n"
            "  2. Under \"Protection history\", Allow / Restore any SoloLuck or cpuminer item.\n"
            "  3. Add an Exclusion (Folder) for:\n"
            "       %s\n"
            "  4. Reopen SoloLuck Miner (it will re-download if needed) and click Start.\n\n"
            "Note: only engine files whose SHA-256 matches the pinned engine manifest run "
            "automatically (audit trail: engine-verify.log in that folder).\n"
            "Advanced: drop your own cpuminer-opt.exe next to this app (you will be "
            "asked to confirm it)." % folder)


class MinerApp:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.reader = None
        self.q = queue.Queue()
        self.accepted = 0
        self.rejected = 0
        self.hashrate = "—"
        self.engine_path = None
        self.engine_ready = False
        self.engine_error = None
        self._start_ts = 0
        self._saw_hash = False
        self._fellback = False          # one automatic retry on the sse2 build per session
        self._user_engine_choice = None  # remembered answer for an unverified user engine
        self._cur_addr = ""
        self._spec = cpu_spec()
        self.meter_src = CpuMeter()
        self._last_cores = None
        self._last_meter_ts = 0
        self._update_info = None
        self._update_path = None
        self._update_downloading = False
        root.title("%s v%s · engine cpuminer-opt %s" % (APP_NAME, APP_VERSION, ENGINE_VERSION))
        root.configure(bg=BG)
        root.minsize(560, 580)
        try:
            self._icon = tk.PhotoImage(data=ICON_B64)
            root.iconphoto(True, self._icon)
        except Exception:
            pass
        self._build_ui()
        self._load_cfg()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self._pump)
        threading.Thread(target=self._init_engine, daemon=True).start()
        threading.Thread(target=self._check_update, daemon=True).start()

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 14, "pady": 4}
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", pady=(14, 6))
        tk.Label(head, text="SoloLuck", fg=ORANGE, bg=BG,
                 font=("Segoe UI", 20, "bold")).pack()
        tk.Label(head, text="Miner — CPU solo mining to sololuck.io", fg=MUTED, bg=BG,
                 font=("Segoe UI", 10)).pack()

        # update banner — created hidden, shown when a newer version is found
        self.update_bar = tk.Frame(self.root, bg="#132015", highlightbackground=GREEN,
                                   highlightthickness=1)
        self.update_lbl = tk.Label(self.update_bar, text="", bg="#132015", fg=GREEN,
                                   font=("Segoe UI", 9, "bold"))
        self.update_lbl.pack(side="left", padx=(12, 8), pady=6)
        self.update_btn = tk.Button(self.update_bar, text="Update now", command=self._do_update,
                                    bg=GREEN, fg="#08120b", relief="flat", cursor="hand2",
                                    font=("Segoe UI", 9, "bold"), padx=12, pady=3)
        self.update_btn.pack(side="right", padx=(0, 10), pady=5)
        _wc = tk.Label(self.update_bar, text="What changed ↗", bg="#132015", fg=MUTED,
                       cursor="hand2", font=("Segoe UI", 8, "underline"))
        _wc.pack(side="right", padx=8)
        _wc.bind("<Button-1>", lambda _e: webbrowser.open(CHANGELOG_URL))

        self._build_cpu_card()

        form = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1, bd=0)
        form.pack(fill="x", padx=14, pady=6)
        inner = tk.Frame(form, bg=CARD)
        inner.pack(fill="x", padx=12, pady=10)

        def row(label, default=""):
            fr = tk.Frame(inner, bg=CARD)
            fr.pack(fill="x", pady=3)
            tk.Label(fr, text=label, fg=MUTED, bg=CARD, width=18, anchor="w",
                     font=("Segoe UI", 9)).pack(side="left")
            var = tk.StringVar(value=default)
            ent = tk.Entry(fr, textvariable=var, bg=BG, fg=FG, insertbackground=FG,
                           relief="flat", font=("Consolas", 10),
                           highlightthickness=1, highlightbackground=BORDER,
                           highlightcolor=ORANGE)
            ent.pack(side="left", fill="x", expand=True, ipady=4)
            return var, ent

        self.addr_var, _ = row("BTC payout address", "")
        self.addr_status = tk.Label(inner, text="", fg=MUTED, bg=CARD, anchor="w",
                                    font=("Segoe UI", 8))
        self.addr_status.pack(fill="x", padx=(122, 0))
        self.addr_var.trace_add("write", lambda *_a: self._on_addr())
        self.worker_var, _ = row("Worker name", "pc")

        pf = tk.Frame(inner, bg=CARD)
        pf.pack(fill="x", pady=3)
        tk.Label(pf, text="Pool", fg=MUTED, bg=CARD, width=18, anchor="w",
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Label(pf, text=POOL_LABEL, fg=FG, bg=CARD, anchor="w",
                 font=("Consolas", 10)).pack(side="left", ipady=4)
        tk.Label(pf, text="  🔒 Nano tier · fixed", fg=MUTED, bg=CARD,
                 font=("Segoe UI", 8)).pack(side="left")

        self._ncpu = os.cpu_count() or 1
        ldf = tk.Frame(inner, bg=CARD)
        ldf.pack(fill="x", pady=(8, 0))
        tk.Label(ldf, text="CPU load", fg=MUTED, bg=CARD, width=18, anchor="w",
                 font=("Segoe UI", 9)).pack(side="left")
        self.pct_var = tk.IntVar(value=CPU_PCT_DEFAULT)
        self.pct_scale = tk.Scale(ldf, from_=CPU_PCT_MIN, to=CPU_PCT_HARD_MAX, orient="horizontal",
                                  variable=self.pct_var, command=self._on_pct,
                                  showvalue=0, bg=ORANGE, fg=FG, troughcolor=BG,
                                  activebackground=ORANGE_HOT, highlightthickness=0,
                                  bd=0, relief="flat", sliderrelief="flat",
                                  sliderlength=22, width=10)
        self.pct_scale.pack(side="left", fill="x", expand=True, padx=(0, 0))
        self.pct_lbl = tk.Label(inner, text="", bg=CARD, anchor="w",
                                font=("Segoe UI", 9, "bold"))
        self.pct_lbl.pack(anchor="w", padx=(118, 0))
        tk.Label(inner, text="Load is capped at 90% so the PC stays usable — the top few "
                 "threads add little hashrate for a lot of heat.",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8), anchor="w",
                 justify="left", wraplength=520).pack(anchor="w", padx=(118, 0))
        # Antivirus shield (Windows only): a quarantined fast engine silently drops
        # you to the slow baseline build — the single biggest hashrate loss — so offer
        # to exclude the engine folder from Windows Defender.
        self.av_frame = tk.Frame(inner, bg=CARD)
        self.av_frame.pack(anchor="w", padx=(118, 0), pady=(3, 0))
        self.av_lbl = tk.Label(self.av_frame, text="", bg=CARD, fg=MUTED,
                               font=("Segoe UI", 8), anchor="w", justify="left", wraplength=430)
        self.av_lbl.pack(side="left")
        self.av_btn = tk.Label(self.av_frame, text="", fg=ORANGE, bg=CARD, cursor="hand2",
                               font=("Segoe UI", 8, "underline"))
        self.av_btn.pack(side="left", padx=(6, 0))
        self.av_btn.bind("<Button-1>", lambda _e: self._shield_av())
        self._refresh_av_ui()
        self._on_pct()
        self._on_addr()

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", **pad)
        self.start_btn = tk.Button(btns, text="▶  Start Mining", command=self.start,
                                   bg=ORANGE, fg="#0b0e14", relief="flat",
                                   font=("Segoe UI", 11, "bold"), activebackground=ORANGE_HOT,
                                   cursor="hand2", padx=16, pady=8)
        self.start_btn.pack(side="left")
        self.stop_btn = tk.Button(btns, text="■  Stop", command=self.stop,
                                  bg=CARD, fg=FG, relief="flat", state="disabled",
                                  font=("Segoe UI", 11, "bold"), activebackground=BORDER,
                                  cursor="hand2", padx=16, pady=8)
        self.stop_btn.pack(side="left", padx=8)

        def hover(btn, normal, hot):
            btn.bind("<Enter>", lambda _e: btn["state"] == "normal" and btn.config(bg=hot))
            btn.bind("<Leave>", lambda _e: btn.config(bg=normal))
        hover(self.start_btn, ORANGE, ORANGE_HOT)
        hover(self.stop_btn, CARD, BORDER)

        stats = tk.Frame(self.root, bg=BG)
        stats.pack(fill="x", **pad)
        self.status_lbl = tk.Label(stats, text="● stopped", fg=MUTED, bg=BG,
                                   font=("Segoe UI", 10, "bold"))
        self.status_lbl.grid(row=0, column=0, sticky="w", columnspan=2, pady=(0, 6))
        self.time_lbl = tk.Label(stats, text="", fg=MUTED, bg=BG, font=("Segoe UI", 9))
        self.time_lbl.grid(row=0, column=2, sticky="e", pady=(0, 6))

        def stat(col, title):
            f = tk.Frame(stats, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
            f.grid(row=1, column=col, sticky="nsew", padx=4)
            stats.grid_columnconfigure(col, weight=1)
            tk.Label(f, text=title, fg=MUTED, bg=CARD, font=("Segoe UI", 8)).pack(pady=(9, 0))
            v = tk.Label(f, text="—", fg=FG, bg=CARD, font=("Segoe UI", 15, "bold"))
            v.pack(pady=(0, 9))
            return v

        self.hr_lbl = stat(0, "HASHRATE")
        self.acc_lbl = stat(1, "ACCEPTED")
        self.rej_lbl = stat(2, "REJECTED")

        # appears after the first accepted share — opens the pool's stats page
        self.link_lbl = tk.Label(self.root, text="", fg=ORANGE, bg=BG, cursor="hand2",
                                 font=("Segoe UI", 9, "underline"))
        self.link_lbl.bind("<Button-1>", self._open_stats)

        self.engine_lbl = tk.Label(self.root, text="Engine: checking…",
                                    fg=MUTED, bg=BG, font=("Segoe UI", 8))
        self.engine_lbl.pack(anchor="w", padx=18, pady=(2, 0))

        logf = tk.Frame(self.root, bg=BG)
        logf.pack(fill="both", expand=True, padx=14, pady=(6, 2))
        tk.Label(logf, text="Miner log", fg=MUTED, bg=BG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        body = tk.Frame(logf, bg=BG, highlightbackground=BORDER, highlightthickness=1)
        body.pack(fill="both", expand=True)
        self.log = tk.Text(body, bg="#080b10", fg=MUTED, relief="flat", wrap="word",
                           font=("Consolas", 9), height=12, insertbackground=FG)
        sb = ttk.Scrollbar(body, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.configure(state="disabled")

        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=16, pady=(4, 10))
        tk.Label(foot, text="SoloLuck Miner v%s · engine cpuminer-opt %s"
                 % (APP_VERSION, ENGINE_VERSION), fg=MUTED, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left")
        self.updchk_lbl = tk.Label(foot, text="Check for updates ↻", fg=ORANGE, bg=BG,
                                   cursor="hand2", font=("Segoe UI", 8, "underline"))
        self.updchk_lbl.pack(side="left", padx=(10, 0))
        self.updchk_lbl.bind("<Button-1>", lambda _e: self._manual_check())
        self.verify_lbl = tk.Label(foot, text="Verify build ✓", fg=ORANGE, bg=BG,
                                   cursor="hand2", font=("Segoe UI", 8, "underline"))
        self.verify_lbl.pack(side="left", padx=(10, 0))
        self.verify_lbl.bind("<Button-1>", lambda _e: self._verify_build())
        wn = tk.Label(foot, text="What's new ↗", fg=ORANGE, bg=BG, cursor="hand2",
                      font=("Segoe UI", 8, "underline"))
        wn.pack(side="right")
        wn.bind("<Button-1>", lambda _e: webbrowser.open(CHANGELOG_URL))

    # ---------- CPU spec card + live per-core meter ----------
    def _build_cpu_card(self):
        spec = self._spec
        card = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1, bd=0)
        card.pack(fill="x", padx=14, pady=6)
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=12, pady=10)

        top = tk.Frame(inner, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text="🖥", bg=CARD, font=("Segoe UI", 15)).pack(side="left", padx=(0, 8))
        nm = tk.Frame(top, bg=CARD)
        nm.pack(side="left", fill="x", expand=True)
        tk.Label(nm, text=spec["brand"], fg=FG, bg=CARD, anchor="w",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        cores = ("%d cores · %d threads" % (spec["physical"], spec["logical"])
                 if spec["physical"] else "%d threads" % spec["logical"])
        sub = cores + (("  ·  mining path: " + spec["tier"]) if spec["tier"] else "")
        tk.Label(nm, text=sub, fg=MUTED, bg=CARD, anchor="w",
                 font=("Segoe UI", 8)).pack(anchor="w")

        row = tk.Frame(inner, bg=CARD)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="Per-core load", fg=MUTED, bg=CARD,
                 font=("Segoe UI", 8)).pack(side="left")
        self.core_count_lbl = tk.Label(row, text="", fg=MUTED, bg=CARD,
                                       font=("Segoe UI", 8))
        self.core_count_lbl.pack(side="right")
        self.meter = tk.Canvas(inner, height=34, bg=BG, highlightthickness=1,
                               highlightbackground=BORDER, bd=0)
        self.meter.pack(fill="x", pady=(3, 0))
        self.meter.bind("<Configure>", lambda _e: self._draw_meter(self._last_cores))
        self.meter.bind("<Enter>", lambda _e: None)

    def _draw_meter(self, pcts):
        c = self.meter
        c.delete("all")
        w = c.winfo_width() or 1
        h = c.winfo_height() or 34
        n = self._spec["logical"]
        if not pcts:
            c.create_text(w // 2, h // 2, text="waiting for CPU data…" if os.name == "nt"
                          else "per-core view is Windows-only",
                          fill=MUTED, font=("Segoe UI", 8))
            return
        pad, gap = 4, 2
        bw = max(2.0, (w - 2 * pad - gap * (n - 1)) / n)
        active = 0
        for i, p in enumerate(pcts[:n]):
            x0 = pad + i * (bw + gap)
            x1 = x0 + bw
            c.create_rectangle(x0, pad, x1, h - pad, fill="#0f141c", outline="")
            fh = (h - 2 * pad) * (p / 100.0)
            # a busy core is GREEN (it's earning); idle cores stay dim
            if p >= 15:
                active += 1
            col = "#243040" if p < 12 else GREEN
            if fh > 0:
                c.create_rectangle(x0, h - pad - fh, x1, h - pad, fill=col, outline="")
        self.core_count_lbl.config(text="%d of %d cores active" % (active, n))

    def _update_meter(self):
        pcts = self.meter_src.sample() if self.meter_src else None
        if pcts:
            self._last_cores = pcts
            self._draw_meter(pcts)

    def _on_addr(self):
        a = self.addr_var.get().strip()
        if not a:
            self.addr_status.config(
                text="A found block pays its whole reward to this address.", fg=MUTED)
            return
        ok, detail = validate_btc_address(a)
        if ok:
            self.addr_status.config(text="✓ Valid Bitcoin address — %s" % detail, fg=GREEN)
        else:
            self.addr_status.config(text="✗ Not a valid Bitcoin address — %s" % detail, fg=RED)

    def _open_stats(self, _event=None):
        if self._cur_addr:
            webbrowser.open("https://sololuck.io/users/%s" % self._cur_addr)

    # ---------- auto-update ----------
    def _check_update(self):
        info = check_for_update()
        if info:
            self.q.put(("__UPDATE__", info))

    def _manual_check(self):
        """Footer 'Check for updates' link — reports both outcomes (the silent
        startup check only surfaces a banner when something newer exists)."""
        self.updchk_lbl.config(text="Checking…")
        threading.Thread(target=self._manual_check_run, daemon=True).start()

    def _manual_check_run(self):
        try:
            info = check_for_update()
        except Exception:
            info = None
            self.q.put(("__NOUPD__", "err"))
            return
        self.q.put(("__UPDATE__", info) if info else ("__NOUPD__", "ok"))

    def _verify_build(self):
        """Footer 'Verify build' link — SHA-256s the running .exe and checks it
        against the checksum published on sololuck.io (fail-loud on mismatch)."""
        self.verify_lbl.config(text="Verifying…")
        threading.Thread(target=self._verify_run, daemon=True).start()

    def _verify_run(self):
        try:
            if not getattr(sys, "frozen", False):
                self.q.put(("__VERIFY__", {"mode": "source"}))
                return
            path = sys.executable
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            res = {"mode": "exe", "path": path, "sha": h.hexdigest(),
                   "app_version": APP_VERSION}
            try:
                latest = _http_get(LATEST_URL, want_json=True, timeout=15)
            except Exception:
                latest = None
            if isinstance(latest, dict):
                res["latest_version"] = latest.get("version")
                res["latest_sha"] = (latest.get("sha256") or "").lower()
            self.q.put(("__VERIFY__", res))
        except Exception as e:
            self.q.put(("__VERIFY__", {"mode": "error", "err": str(e)}))

    def _on_verify_result(self, res):
        self.verify_lbl.config(text="Verify build ✓")
        mode = res.get("mode")
        if mode == "source":
            messagebox.showinfo("Verify build",
                "You're running from source, so there's no released .exe to verify.\n\n"
                "Download the signed release from sololuck.io/setup, or read the source "
                "on GitHub: github.com/sololuckio/sololuck")
            return
        if mode == "error":
            messagebox.showwarning("Verify build", "Couldn't verify this file:\n%s" % res.get("err"))
            return
        sha, av = res["sha"], res["app_version"]
        lv, ls = res.get("latest_version"), res.get("latest_sha")
        head = "This file:\n  %s\n\nSHA-256:\n  %s\n\n" % (res["path"], sha)
        if lv and av == lv and ls:
            if sha == ls:
                messagebox.showinfo("Verify build ✓", head +
                    "✓ MATCH — this is the authentic SoloLuck Miner v%s.\n"
                    "Its checksum matches sololuck.io exactly." % av)
            else:
                messagebox.showerror("Verify build ✗", head +
                    "✗ MISMATCH — this file does NOT match the published v%s checksum!\n"
                    "  published: %s\n\n"
                    "Do not trust this file. Re-download from sololuck.io/setup." % (av, ls))
        else:
            messagebox.showinfo("Verify build", head +
                "Your build: v%s   (site's current release: v%s)\n\n"
                "Compare the SHA-256 above against the published checksum for v%s at:\n"
                "  sololuck.io/SHA256SUMS.txt\n"
                "  or the GitHub release: github.com/sololuckio/sololuck/releases"
                % (av, lv or "?", av))

    def _start_download(self):
        """Fetch + verify the update in the background (only meaningful frozen)."""
        if not getattr(sys, "frozen", False) or self._update_downloading:
            return
        self._update_downloading = True
        threading.Thread(target=self._run_update, args=(self._update_info,), daemon=True).start()

    def _run_update(self, info):
        try:
            path = download_update(info, lambda s: self.q.put(("__ENGMSG__", s)))
        except Exception as e:
            self.q.put(("__UPDERR__", str(e)))
            return
        self.q.put(("__UPDREADY__", path))

    def _do_update(self, _event=None):
        """Banner button. Source builds open the download page; frozen builds
        apply the verified update now (or the moment the download finishes)."""
        if not getattr(sys, "frozen", False):
            webbrowser.open("https://sololuck.io/setup")
            return
        if self._update_path:
            self._apply_update()
        else:
            self._start_download()
            self.update_btn.config(state="disabled", text="Downloading…")

    def _apply_update(self):
        """Stop mining if needed, launch the verified new exe, and close."""
        if not self._update_path:
            return
        self.stop(user=False)
        self._logln("Applying update — restarting into the new version…", GREEN)
        try:
            subprocess.Popen([self._update_path],
                             cwd=os.path.dirname(self._update_path) or None)
            self.root.after(400, self.on_close)
        except Exception as e:
            self.update_btn.config(state="normal", text="Update now")
            messagebox.showerror(APP_NAME, "Couldn't launch the update:\n%s\n\n"
                                 "Download it from sololuck.io/setup." % e)

    # ---------- CPU load slider ----------
    def _on_pct(self, _value=None):
        pct = self.pct_var.get()
        # hard cap: the slider tops out at 90%, but clamp anyway in case an older
        # saved config or a stray value pushed it higher.
        if pct > CPU_PCT_HARD_MAX:
            pct = CPU_PCT_HARD_MAX
            self.pct_var.set(pct)
        t = threads_for(pct, self._ncpu)
        # green while inside the recommended threshold, amber above it (up to the cap)
        safe = pct <= CPU_PCT_SOFT_MAX
        tag = "✓ recommended" if safe else "⚠ high load"
        self.pct_lbl.config(text="%d%% · %d of %d threads · %s" % (pct, t, self._ncpu, tag),
                            fg=GREEN if safe else ORANGE)

    # ---------- antivirus shield ----------
    def _refresh_av_ui(self):
        """Show the shield row only on Windows, and only nag when not yet excluded."""
        present = av_exclusion_present()
        if present is None:                 # not Windows / can't tell → hide entirely
            self.av_frame.pack_forget()
            return
        if present:
            self.av_lbl.config(text="🛡 Mining engine is shielded from antivirus ✓", fg=GREEN)
            self.av_btn.config(text="")
        else:
            self.av_lbl.config(
                text="🛡 Shield the engine so antivirus can't quarantine it (keeps you on "
                     "the fast build). Real-time protection stays ON — only this folder "
                     "is excluded. No need to turn anything off.", fg=ORANGE)
            self.av_btn.config(text="Shield it")

    def _shield_av(self):
        if not self.av_btn.cget("text") or getattr(self, "_shielding", False):
            return
        self._shielding = True
        self.av_btn.config(text="Shielding…")
        self._logln("Approve the Windows prompt to exclude the mining-engine folder. "
                    "Real-time protection stays on — only this one folder is skipped.", GREEN)
        threading.Thread(target=self._shield_av_run, daemon=True).start()

    def _shield_av_run(self):
        ok = add_av_exclusion()          # blocks on the UAC prompt (off the UI thread)
        self.q.put(("__SHIELD__", ok))

    def _on_shield_result(self, ok):
        self._shielding = False
        if ok:
            self._logln("Shielded ✓ — the fast engine won't be quarantined. If you were on "
                        "the slow build, click Stop then Start to pick up the fast one.", GREEN)
        else:
            self._logln("Couldn't add the exclusion (prompt declined?). In Windows Security → "
                        "Virus & threat protection → Manage settings → Exclusions, add this "
                        "folder:\n%s" % engine_dir(), ORANGE)
        self._refresh_av_ui()

    def _logln(self, text, color=None):
        self.log.configure(state="normal")
        if color:
            tag = "c_" + color.lstrip("#")
            self.log.tag_configure(tag, foreground=color)
            self.log.insert("end", text + "\n", tag)
        else:
            self.log.insert("end", text + "\n")
        self.log.see("end")
        if int(self.log.index("end-1c").split(".")[0]) > 600:
            self.log.delete("1.0", "200.0")
        self.log.configure(state="disabled")

    # ---------- engine resolution (off the UI thread) ----------
    def _engine_ok(self, path, why):
        self.engine_path = path
        self.engine_ready = True
        self.engine_error = None
        self.q.put(("__ENG__", os.path.basename(path), why))

    def _init_engine(self):
        """Same trust rules as ensure_engine(), but the 'run YOUR unverified
        file?' question is bounced to the UI thread via the queue."""
        try:
            over = find_user_miner()
            if over:
                if verify_engine_file(over):
                    self._engine_ok(over, "your copy — SHA-256 verified")
                    return
                if self._user_engine_choice is True:
                    self._engine_ok(over, "your own build (unverified — you approved it)")
                    return
                if self._user_engine_choice is None:
                    self.q.put(("__CONFIRM__", over))
                    return  # resolution continues after the user answers
            # accept a cached engine only if it's the BEST build this CPU can run;
            # if a faster build is missing (e.g. antivirus removed it, leaving only
            # the slow sse2 baseline) re-fetch it — download_engine() shields the
            # folder first so the refetch survives with real-time protection ON.
            local = find_local_engine()
            ideal = (preferred_builds() or [None])[0]
            ideal_ok = bool(ideal and os.path.isfile(os.path.join(engine_dir(), ideal))
                            and verify_engine_file(os.path.join(engine_dir(), ideal)))
            if local and ideal_ok:
                self._engine_ok(local, "SHA-256 verified")
                return
            try:
                p = download_engine(lambda s: self.q.put(("__ENGMSG__", s)))
                self._engine_ok(p, "downloaded + SHA-256 verified")
            except Exception:
                if local:   # refetch failed but a slower verified build is present
                    self._engine_ok(local, "SHA-256 verified (baseline — ‘Shield it’ for full speed)")
                    return
                raise
        except Exception as e:
            self.engine_error = str(e)
            self.q.put(("__ENGERR__", str(e)))

    def _tier_from_name(self, name):
        n = name.lower()
        if "avx512" in n:
            return "AVX-512"
        if "avx2-sha" in n:
            return "AVX2 + SHA"
        if "avx2" in n:
            return "AVX2"
        if "aes-sse42" in n or "sse42" in n:
            return "SSE4.2 + AES"
        if "sse2" in n:
            return "SSE2 (baseline)"
        return ""

    # ---------- config ----------
    def _load_cfg(self):
        try:
            with open(CFG_PATH) as f:
                c = json.load(f)
            self.addr_var.set(c.get("addr", ""))
            self.worker_var.set(c.get("worker", "pc"))
            if "cpu_pct" in c:
                pct = int(c["cpu_pct"])
                # migrate v1.7 and earlier: full_cpu=true (or any >90%) meant 100% —
                # now hard-capped at 90%.
                if c.get("full_cpu"):
                    pct = CPU_PCT_HARD_MAX
            elif str(c.get("threads", "")).isdigit():
                # pre-1.3.0 cfg stored a thread count — map it onto the slider
                pct = int(round(int(c["threads"]) * 100.0 / self._ncpu))
            else:
                pct = CPU_PCT_DEFAULT
            self.pct_var.set(max(CPU_PCT_MIN, min(CPU_PCT_HARD_MAX, pct)))
            self._on_pct()
        except Exception:
            pass

    def _save_cfg(self):
        try:
            with open(CFG_PATH, "w") as f:
                json.dump({"addr": self.addr_var.get().strip(),
                           "worker": self.worker_var.get().strip(),
                           "cpu_pct": self.pct_var.get()}, f)
        except Exception:
            pass

    # ---------- mining control ----------
    def _resolve_start_engine(self):
        """Engine for this Start click. A user-dropped build still wins, but only
        SHA-256-verified — or after the same explicit yes the init path uses
        (remembered for the session). After an auto-fallback, stick to it."""
        if self._fellback:
            return self.engine_path
        up = find_user_miner()
        if up and up != self.engine_path:
            if verify_engine_file(up):
                return up
            if self._user_engine_choice is None:
                self._user_engine_choice = _confirm_unverified_user_engine(up)
                _verify_log("user-supplied engine %s unverified -> %s"
                            % (up, "user accepted" if self._user_engine_choice else "refused"))
            if self._user_engine_choice:
                return up
        return self.engine_path or find_local_engine()

    def start(self):
        if self.proc:
            return
        addr = self.addr_var.get().strip()
        worker = re.sub(r"[^A-Za-z0-9_-]", "", self.worker_var.get().strip())
        host, port = DEFAULT_HOST, DEFAULT_PORT   # pool endpoint is fixed
        self._on_pct()  # sync the label with the current slider value
        # the load is hard-capped at 90% — clamp here too so nothing (a stale cfg,
        # a stray value) can ever push the miner above the cap.
        eff_pct = min(self.pct_var.get(), CPU_PCT_HARD_MAX)
        threads = str(threads_for(eff_pct, self._ncpu))

        ok, detail = validate_btc_address(addr)
        if not ok:
            messagebox.showerror(APP_NAME,
                "That is not a valid Bitcoin address (%s).\n\nUse the address you want the "
                "block reward paid to (e.g. bc1q…). In solo mode the address IS your "
                "login — a typo here would make a found block unclaimable." % detail)
            return

        miner = self._resolve_start_engine()
        if not miner or not os.path.isfile(miner):
            if not self.engine_ready and not self.engine_error:
                messagebox.showinfo(APP_NAME,
                    "The mining engine is still downloading (one-time, ~18 MB). "
                    "Give it a moment, then click Start again.")
            elif self.engine_error and messagebox.askretrycancel(
                    APP_NAME, _engine_missing_msg() + "\n\nRetry the download now?"):
                self.engine_error = None
                self.engine_lbl.config(text="Engine: downloading…", fg=MUTED)
                threading.Thread(target=self._init_engine, daemon=True).start()
            elif not self.engine_error:
                messagebox.showerror(APP_NAME, _engine_missing_msg())
            return
        self.engine_path = miner

        user = ("%s.%s" % (addr, worker)) if worker else addr
        url = "stratum+tcp://%s:%s" % (host, port)
        cmd = [miner, "-a", ALGO, "-o", url, "-u", user, "-p", "x"]
        if threads.isdigit():
            cmd += ["-t", threads]

        self.accepted = 0
        self.rejected = 0
        self.acc_lbl.config(text="0")
        self.rej_lbl.config(text="0")
        self.hr_lbl.config(text="…")
        self._saw_hash = False
        self._start_ts = time.time()
        self._cur_addr = addr
        self._save_cfg()
        self._logln("$ %s -a %s -o %s -u %s -p x%s"
                    % (os.path.basename(miner), ALGO, url, user,
                       (" -t " + threads) if threads.isdigit() else ""), ORANGE)

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                universal_newlines=True, creationflags=creationflags,
                cwd=app_dir())
        except OSError as e:
            self.proc = None
            if isinstance(e, FileNotFoundError) or getattr(e, "winerror", None) in (2, 225, 226):
                messagebox.showerror(APP_NAME, _engine_missing_msg())
            else:
                messagebox.showerror(APP_NAME, "Couldn't launch the miner:\n%s" % e)
            return
        except Exception as e:
            self.proc = None
            messagebox.showerror(APP_NAME, "Couldn't launch the miner:\n%s" % e)
            return

        self.reader = threading.Thread(target=self._read_output, args=(self.proc,), daemon=True)
        self.reader.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_lbl.config(text="● connecting…", fg=ORANGE)

    def _read_output(self, p):
        try:
            for line in p.stdout:
                self.q.put(line.rstrip("\n"))
        except Exception:
            pass
        self.q.put(("__EXIT__", p.poll(), p))

    def stop(self, user=True):
        p, self.proc = self.proc, None
        if p:
            # terminate now; wait/kill off the UI thread so the window never freezes
            try:
                p.terminate()
            except Exception:
                pass
            def _reap():
                try:
                    p.wait(timeout=4)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
            threading.Thread(target=_reap, daemon=True).start()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_lbl.config(text="● stopped", fg=MUTED)
        self.hr_lbl.config(text="—")
        if user:
            self._logln("— stopped —", MUTED)
            # a verified update was waiting for the miner to stop → apply it now
            if self._update_path:
                self.root.after(300, self._apply_update)

    # ---------- events / output ----------
    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple) and item:
                    self._handle_event(item)
                    continue
                self._handle_line(item)
        except queue.Empty:
            pass
        if self.proc is not None and self._start_ts:
            s = int(time.time() - self._start_ts)
            self.time_lbl.config(text="⏱ %d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60))
        elif self.time_lbl.cget("text"):
            self.time_lbl.config(text="")
        now = time.time()
        if now - self._last_meter_ts >= 1.0:   # CPU% needs a ~1s delta window
            self._last_meter_ts = now
            self._update_meter()
        self.root.after(200, self._pump)

    def _handle_event(self, item):
        kind = item[0]
        if kind == "__EXIT__":
            if len(item) > 2 and item[2] is not self.proc:
                return  # a previous run's reader finishing — not the live miner
            if self.proc is not None:
                code = item[1]
                self._logln("— miner exited (code %s%s) —" % (code, explain_exit(code)), RED)
                if self._try_sse2_fallback(code):
                    return
                self.stop(user=False)
        elif kind == "__ENG__":
            name, why = item[1], item[2]
            tier = self._tier_from_name(name)
            self.engine_lbl.config(
                text="Engine: %s%s · %s" % (name, (" (" + tier + ")") if tier else "", why),
                fg=MUTED)
            self._logln("Engine ready: %s%s [%s]"
                        % (name, (" — " + tier) if tier else "", why), MUTED)
        elif kind == "__ENGMSG__":
            self.engine_lbl.config(text=item[1], fg=MUTED)
            self._logln(item[1], MUTED)
        elif kind == "__ENGERR__":
            self.engine_lbl.config(text="Engine: unavailable — press Start for help", fg=RED)
            self._logln("Engine could not be prepared: %s" % item[1], RED)
        elif kind == "__CONFIRM__":
            self._user_engine_choice = _confirm_unverified_user_engine(item[1])
            _verify_log("user-supplied engine %s unverified -> %s"
                        % (item[1], "user accepted" if self._user_engine_choice else "refused"))
            threading.Thread(target=self._init_engine, daemon=True).start()
        elif kind == "__SHIELD__":
            self._on_shield_result(item[1])
        elif kind == "__VERIFY__":
            self._on_verify_result(item[1])
        elif kind == "__NOUPD__":
            self.updchk_lbl.config(
                text="Up to date ✓" if item[1] == "ok" else "Check failed — retry ↻")
            self.root.after(4000, lambda: self.updchk_lbl.config(text="Check for updates ↻"))
        elif kind == "__UPDATE__":
            self.updchk_lbl.config(text="Check for updates ↻")
            self._update_info = item[1]
            ver = item[1]["version"]
            self.update_bar.pack(fill="x", padx=14, pady=(0, 4),
                                 after=self.root.winfo_children()[0])
            if getattr(sys, "frozen", False):
                # auto-download+verify in the background straight away
                self.update_lbl.config(text="⬆ v%s available — downloading…" % ver)
                self.update_btn.config(state="disabled", text="Downloading…")
                self._start_download()
            else:
                self.update_lbl.config(text="⬆ SoloLuck Miner v%s is available" % ver)
                self.update_btn.config(text="Get it")
            self._logln("A newer version (v%s) is available." % ver, GREEN)
        elif kind == "__UPDREADY__":
            self._update_path = item[1]
            self.update_btn.config(state="normal", text="Update now")
            if self.proc is None:
                self._logln("Update verified — installing now…", GREEN)
                self._apply_update()       # idle → auto-install
            else:
                self.update_lbl.config(
                    text="⬆ v%s downloaded — installs when you Stop" % self._update_info["version"])
                self._logln("Update downloaded and verified — it will install when you stop "
                            "mining, or click Update now.", GREEN)
        elif kind == "__UPDERR__":
            self._update_downloading = False
            self.update_btn.config(state="normal", text="Retry update")
            self._logln("Update failed: %s" % item[1], RED)

    def _try_sse2_fallback(self, code):
        """A too-new build crashing on launch (illegal instruction) auto-retries
        once on the universal sse2 build instead of leaving a cryptic error."""
        ran_short = time.time() - self._start_ts < 12
        if (self._fellback or self._saw_hash or not ran_short
                or not is_cpu_mismatch_exit(code)):
            return False
        cur = os.path.basename(self.engine_path or "")
        if cur == "cpuminer-sse2.exe":
            return False
        sse2 = os.path.join(engine_dir(), "cpuminer-sse2.exe")
        if not (os.path.isfile(sse2) and verify_engine_file(sse2)):
            return False
        self._fellback = True
        self.engine_path = sse2
        self.stop(user=False)
        self._logln("That engine build crashed right away — your CPU may not support it. "
                    "Retrying with the baseline SSE2 build (works on any 64-bit CPU)…", ORANGE)
        self.start()
        return True

    def _handle_line(self, line):
        low = line.lower()
        color = None
        state = classify_line(line)
        if "accepted" in low or "yes!" in low:
            color = GREEN
            self.status_lbl.config(text="● mining", fg=GREEN)
        elif "rejected" in low or "booo" in low:
            color = RED
        elif state == "fail":
            color = RED
            self.status_lbl.config(text="● reconnecting…", fg=ORANGE)
        elif state == "live":
            self.status_lbl.config(text="● mining", fg=GREEN)

        m = ACCEPT_RE.search(line)
        if m:
            acc, total = int(m.group(1)), int(m.group(2))
            self.accepted = acc
            self.rejected = max(0, total - acc)
            self.acc_lbl.config(text=str(self.accepted))
            self.rej_lbl.config(text=str(self.rejected))
        elif "accepted" in low:
            self.accepted += 1
            self.acc_lbl.config(text=str(self.accepted))
        elif "rejected" in low:
            self.rejected += 1
            self.rej_lbl.config(text=str(self.rejected))
        if self.accepted > 0 and self._cur_addr and not self.link_lbl.winfo_ismapped():
            self.link_lbl.config(text="Share accepted — see your worker at "
                                      "sololuck.io/users/%s…" % self._cur_addr[:12])
            self.link_lbl.pack(anchor="w", padx=18, pady=(2, 0),
                               before=self.engine_lbl)

        if "h/s" in low and not low.lstrip("[0123456789:.\\- ]").startswith("cpu #"):
            hm = HASH_RE.search(line)
            if hm:
                self._saw_hash = True
                self.hashrate = "%s %sH/s" % (hm.group(1), hm.group(2) or "")
                self.hr_lbl.config(text=self.hashrate)

        self._logln(line, color)

    def on_close(self):
        self.stop(user=False)
        self.root.destroy()


def _selftest():
    """Headless build-validation: resolve the engine (downloading if needed) and run
    it with -V to confirm it launches. Writes the result to a file beside the app."""
    out = os.path.join(app_dir(), "sololuck_selftest.txt")
    lines = []
    def w(s):
        lines.append(str(s))
    try:
        w("features: %s" % cpu_features())
        w("preferred: %s" % preferred_builds())
        w("engine_dir: %s" % engine_dir())
        eng = ensure_engine(lambda s: w("  " + s))
        w("engine: %s" % eng)
        if not eng:
            w("RESULT: FAIL (no engine)")
        else:
            w("engine_files: %s" % sorted(os.listdir(os.path.dirname(eng))))
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
            r = subprocess.run([eng, "-V"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=30, creationflags=flags, text=True)
            w("engine -V exit: %s" % r.returncode)
            for ln in (r.stdout or "").splitlines()[:5]:
                w("  " + ln)
            ok = r.returncode == 0 and "cpuminer" in (r.stdout or "").lower()
            w("RESULT: %s" % ("PASS — engine downloaded + launches" if ok else "FAIL"))
    except Exception as e:
        w("error: %r" % e)
        w("RESULT: FAIL")
    try:
        open(out, "w").write("\n".join(lines) + "\n")
    except Exception:
        pass


def _minetest(seconds, addr, threads):
    """Headless end-to-end test: resolve the engine and mine live for `seconds`,
    exactly like the Start button. Writes result beside the app.
    Usage: SoloLuckMiner.exe --minetest <seconds> <btc-address> [threads]"""
    out = os.path.join(app_dir(), "sololuck_minetest.txt")
    log = []
    def w(s):
        log.append(str(s))
    if not validate_btc_address(addr or "")[0]:
        w("bad or missing BTC address %r" % addr); w("RESULT: FAIL")
        open(out, "w").write("\n".join(log) + "\n"); return
    try:
        eng = ensure_engine(lambda s: w(s))
    except Exception as e:
        w("engine error: %r" % e); w("RESULT: FAIL")
        open(out, "w").write("\n".join(log) + "\n"); return
    w("engine: %s" % eng)
    url = "stratum+tcp://%s:%s" % (DEFAULT_HOST, DEFAULT_PORT)
    cmd = [eng, "-a", ALGO, "-o", url, "-u", "%s.%s" % (addr, "bundletest"), "-p", "x", "-t", str(threads)]
    w("cmd: %s" % " ".join(cmd))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
    captured = []
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, creationflags=flags)
    except Exception as e:
        w("launch error: %r" % e); w("RESULT: FAIL")
        open(out, "w").write("\n".join(log) + "\n"); return
    threading.Thread(target=lambda: [captured.append(l.rstrip()) for l in p.stdout], daemon=True).start()
    end = time.time() + seconds
    while time.time() < end and p.poll() is None:
        time.sleep(0.5)
    try:
        p.terminate(); p.wait(timeout=5)
    except Exception:
        try: p.kill()
        except Exception: pass
    low = "\n".join(captured).lower()
    connected = "stratum" in low and ("connect" in low or "subscrib" in low or "authoriz" in low or "difficulty" in low)
    gotwork = "new work" in low or "new job" in low or "stratum diff" in low
    hashing = "h/s" in low or "hash rate" in low
    w("--- last 25 lines ---")
    for ln in captured[-25:]:
        w("  " + ln)
    w("--- signals: connected=%s gotwork=%s hashing=%s" % (connected, gotwork, hashing))
    w("RESULT: %s" % ("PASS — mining live to the pool" if (connected and (gotwork or hashing)) else "FAIL"))
    try:
        open(out, "w").write("\n".join(log) + "\n")
    except Exception:
        pass


def _cpuinfo():
    """Headless dump of what the spec card would show + a per-core meter sample.
    Usage: SoloLuckMiner.exe --cpuinfo"""
    out = os.path.join(app_dir(), "sololuck_cpuinfo.txt")
    lines = []
    def w(s):
        lines.append(str(s)); print(s)
    sp = cpu_spec()
    w("brand: %s" % sp["brand"])
    w("physical_cores: %s" % sp["physical"])
    w("logical_threads: %s" % sp["logical"])
    w("mining_path: %s (%s)" % (sp["tier"], sp["build"]))
    w("features: %s" % cpu_features())
    m = CpuMeter()
    m.sample(); time.sleep(1.0)
    pcts = m.sample()
    w("per_core_sample: %s" % pcts)
    w("update_check: %s" % (check_for_update() or "none/uptodate"))
    ok = bool(sp["brand"] and sp["logical"] >= 1 and sp["tier"])
    w("RESULT: %s" % ("PASS" if ok else "FAIL"))
    try:
        open(out, "w").write("\n".join(lines) + "\n")
    except Exception:
        pass


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    if "--cpuinfo" in sys.argv:
        _cpuinfo()
        return
    if "--minetest" in sys.argv:
        i = sys.argv.index("--minetest")
        rest = sys.argv[i + 1:]
        secs = int(rest[0]) if len(rest) > 0 and rest[0].isdigit() else 90
        addr = rest[1] if len(rest) > 1 else ""
        thr = int(rest[2]) if len(rest) > 2 and rest[2].isdigit() else 2
        _minetest(secs, addr, thr)
        return
    if os.name == "nt":
        # crisp text on high-DPI displays (Tk then picks up the real DPI itself)
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    root = tk.Tk()
    try:
        if float(root.tk.call("tk", "scaling")) < 1.2:
            root.tk.call("tk", "scaling", 1.2)
    except Exception:
        pass
    MinerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
