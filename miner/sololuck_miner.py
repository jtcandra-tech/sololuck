#!/usr/bin/env python3
"""
SoloLuck Miner — a simple Start/Stop GUI that drives the cpuminer-opt CPU engine
and points it at the SoloLuck solo Bitcoin pool (sololuck.io).

A PC's hashrate is tiny next to an ASIC, so this is a lottery ticket — which is
exactly the point of a solo pool. If your CPU happens to solve a block, the whole
reward is paid straight to your address on-chain (minus the pool's flat 2% fee).

This is a CLEAN WRAPPER: it contains no mining code itself. On first run it detects
your CPU and DOWNLOADS the matching cpuminer-opt build (Jay D Dee's, GPLv2) from
GitHub into a folder next to the app, then runs it. Nothing to install. (Advanced
users can drop their own cpuminer-opt.exe next to this app to skip the download.)

Why a clean wrapper: a GUI with no embedded miner isn't itself flagged as a coin
miner, so the app always launches; antivirus only ever flags the downloaded engine,
which you whitelist once. Pure Python standard library (tkinter + urllib).
"""
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
import zipfile
import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "SoloLuck Miner"
DEFAULT_HOST = "sololuck.io"
DEFAULT_PORT = "3335"   # Nano tier — diff 1, tuned for CPUs so shares register fast
ALGO = "sha256d"   # Bitcoin
ENGINE_DIR_NAME = "SoloLuckMiner-engine"
GITHUB_RELEASE_API = "https://api.github.com/repos/JayDDee/cpuminer-opt/releases/latest"
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
ADDR_RE = re.compile(r"^(bc1[a-z0-9]{20,90}|[13][a-km-zA-HJ-NP-Z1-9]{20,40})$")
HASH_RE = re.compile(r"([\d.]+)\s*([kKMGTP]?)[hH]/s")
ACCEPT_RE = re.compile(r"[Aa]ccepted\s+(\d+)/(\d+)")

# colours (brand-ish, works on the default tk theme)
BG = "#0b0e14"
CARD = "#11161f"
FG = "#dfe6f0"
MUTED = "#9fb0c5"
ORANGE = "#f7931a"
GREEN = "#3ad17a"
RED = "#ff6b6b"


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
    if f["SSE42"] and f["AES"]:
        c.append("cpuminer-aes-sse42.exe")
    c.append("cpuminer-sse2.exe")
    seen, out = set(), []
    for x in c:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


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


def find_local_engine():
    """The best already-downloaded build in the engine folder, or None."""
    d = engine_dir()
    for b in preferred_builds():
        p = os.path.join(d, b)
        if os.path.isfile(p):
            return p
    return None


def _http_get(url, want_json=False, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "SoloLuckMiner",
                                               "Accept": "application/octet-stream" if not want_json
                                               else "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data.decode("utf-8")) if want_json else data


def _latest_engine_zip_url():
    rel = _http_get(GITHUB_RELEASE_API, want_json=True, timeout=60)
    assets = rel.get("assets", []) or []
    for a in assets:
        n = (a.get("name") or "").lower()
        if n.endswith(".zip") and ("windows" in n or "win64" in n):
            return a.get("browser_download_url")
    for a in assets:
        if (a.get("name") or "").lower().endswith(".zip"):
            return a.get("browser_download_url")
    raise RuntimeError("No Windows engine archive on the latest cpuminer-opt release.")


def download_engine(report=lambda s: None):
    """Fetch the cpuminer-opt Windows archive from GitHub and extract the build that
    fits this CPU (+ sse2 fallback + runtime DLLs) into the engine folder. Returns a
    runnable engine path. Raises on network/extract/AV failure."""
    dest = engine_dir()
    report("Finding the latest mining engine…")
    url = _latest_engine_zip_url()
    report("Downloading the mining engine (~18 MB, one time)…")
    blob = _http_get(url, timeout=300)
    report("Unpacking…")
    zf = zipfile.ZipFile(io.BytesIO(blob))
    members = {os.path.basename(n): n for n in zf.namelist() if not n.endswith("/")}
    wanted = []
    for b in preferred_builds():
        if b in members:
            wanted.append(b)
            break
    if "cpuminer-sse2.exe" in members and "cpuminer-sse2.exe" not in wanted:
        wanted.append("cpuminer-sse2.exe")
    for d in ENGINE_DLLS:
        if d in members:
            wanted.append(d)
    if not wanted:
        raise RuntimeError("The engine archive didn't contain the expected files.")
    os.makedirs(dest, exist_ok=True)
    for name in wanted:
        with zf.open(members[name]) as src, open(os.path.join(dest, name), "wb") as out:
            out.write(src.read())
    eng = find_local_engine()
    if not eng:
        raise RuntimeError("Engine unpacked but no runnable build was produced.")
    return eng


def ensure_engine(report=lambda s: None):
    """Resolve a runnable engine: user override → already-downloaded → download now."""
    return find_user_miner() or find_local_engine() or download_engine(report)


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
            "Advanced: drop your own cpuminer-opt.exe next to this app to skip the download." % folder)


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
        self._cfg_engine = ""
        self._cfg_sig = ""
        self._start_ts = 0
        self._saw_hash = False
        root.title(APP_NAME)
        root.configure(bg=BG)
        root.minsize(560, 580)
        self._build_ui()
        self._load_cfg()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self._pump)
        threading.Thread(target=self._init_engine, daemon=True).start()

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 14, "pady": 4}
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", pady=(14, 6))
        tk.Label(head, text="SoloLuck", fg=ORANGE, bg=BG,
                 font=("Segoe UI", 20, "bold")).pack()
        tk.Label(head, text="Miner — CPU solo mining to sololuck.io", fg=MUTED, bg=BG,
                 font=("Segoe UI", 10)).pack()

        form = tk.Frame(self.root, bg=BG)
        form.pack(fill="x", **pad)

        def row(label, default="", show=None, width=44):
            fr = tk.Frame(form, bg=BG)
            fr.pack(fill="x", pady=3)
            tk.Label(fr, text=label, fg=MUTED, bg=BG, width=16, anchor="w",
                     font=("Segoe UI", 9)).pack(side="left")
            var = tk.StringVar(value=default)
            ent = tk.Entry(fr, textvariable=var, bg=CARD, fg=FG, insertbackground=FG,
                           relief="flat", width=width, font=("Consolas", 10))
            if show:
                ent.config(show=show)
            ent.pack(side="left", fill="x", expand=True, ipady=4)
            return var, ent

        self.addr_var, _ = row("BTC payout address", "")
        self.worker_var, _ = row("Worker name", "pc")
        self.host_var, _ = row("Pool host", DEFAULT_HOST)
        self.port_var, _ = row("Port", DEFAULT_PORT)
        self.threads_var, _ = row("CPU threads", "")
        tk.Label(form, text="(threads blank = auto / all cores)", fg=MUTED, bg=BG,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=(120, 0))

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", **pad)
        self.start_btn = tk.Button(btns, text="▶  Start Mining", command=self.start,
                                   bg=ORANGE, fg="#0b0e14", relief="flat",
                                   font=("Segoe UI", 11, "bold"), activebackground="#ffa733",
                                   cursor="hand2", padx=16, pady=8)
        self.start_btn.pack(side="left")
        self.stop_btn = tk.Button(btns, text="■  Stop", command=self.stop,
                                  bg=CARD, fg=FG, relief="flat", state="disabled",
                                  font=("Segoe UI", 11, "bold"), cursor="hand2",
                                  padx=16, pady=8)
        self.stop_btn.pack(side="left", padx=8)

        stats = tk.Frame(self.root, bg=BG)
        stats.pack(fill="x", **pad)
        self.status_lbl = tk.Label(stats, text="● stopped", fg=MUTED, bg=BG,
                                   font=("Segoe UI", 10, "bold"))
        self.status_lbl.grid(row=0, column=0, sticky="w", columnspan=3, pady=(0, 6))

        def stat(col, title):
            f = tk.Frame(stats, bg=CARD)
            f.grid(row=1, column=col, sticky="nsew", padx=4)
            stats.grid_columnconfigure(col, weight=1)
            tk.Label(f, text=title, fg=MUTED, bg=CARD, font=("Segoe UI", 8)).pack(pady=(8, 0))
            v = tk.Label(f, text="—", fg=FG, bg=CARD, font=("Segoe UI", 14, "bold"))
            v.pack(pady=(0, 8))
            return v

        self.hr_lbl = stat(0, "HASHRATE")
        self.acc_lbl = stat(1, "ACCEPTED")
        self.rej_lbl = stat(2, "REJECTED")

        self.engine_lbl = tk.Label(self.root, text="Engine: checking…",
                                    fg=MUTED, bg=BG, font=("Segoe UI", 8))
        self.engine_lbl.pack(anchor="w", padx=18, pady=(2, 0))

        logf = tk.Frame(self.root, bg=BG)
        logf.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        tk.Label(logf, text="Miner log", fg=MUTED, bg=BG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self.log = tk.Text(logf, bg="#080b10", fg=MUTED, relief="flat", wrap="word",
                           font=("Consolas", 9), height=12, insertbackground=FG)
        self.log.pack(fill="both", expand=True)
        sb = ttk.Scrollbar(self.log, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.configure(state="disabled")

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
    def _init_engine(self):
        try:
            over = find_user_miner()
            if over:
                self.engine_path = over
                self.engine_ready = True
                self.q.put(("__ENG__", os.path.basename(over), "your own build"))
                return
            local = find_local_engine()
            if local:
                self.engine_path = local
                self.engine_ready = True
                self.q.put(("__ENG__", os.path.basename(local), "ready"))
                return
            p = download_engine(lambda s: self.q.put(("__ENGMSG__", s)))
            self.engine_path = p
            self.engine_ready = True
            self._cfg_engine = os.path.basename(p)
            self._cfg_sig = cpu_signature()
            self.q.put(("__ENG__", os.path.basename(p), "downloaded"))
            self.q.put(("__SAVECFG__", None))
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
            self.host_var.set(c.get("host", DEFAULT_HOST))
            self.port_var.set(c.get("port", DEFAULT_PORT))
            self.threads_var.set(c.get("threads", ""))
            self._cfg_engine = c.get("engine", "")
            self._cfg_sig = c.get("engine_sig", "")
        except Exception:
            pass

    def _save_cfg(self):
        try:
            with open(CFG_PATH, "w") as f:
                json.dump({"addr": self.addr_var.get().strip(),
                           "worker": self.worker_var.get().strip(),
                           "host": self.host_var.get().strip(),
                           "port": self.port_var.get().strip(),
                           "threads": self.threads_var.get().strip(),
                           "engine": self._cfg_engine,
                           "engine_sig": self._cfg_sig}, f)
        except Exception:
            pass

    # ---------- mining control ----------
    def start(self):
        if self.proc:
            return
        addr = self.addr_var.get().strip()
        worker = re.sub(r"[^A-Za-z0-9_-]", "", self.worker_var.get().strip())
        host = self.host_var.get().strip() or DEFAULT_HOST
        port = self.port_var.get().strip() or DEFAULT_PORT
        threads = self.threads_var.get().strip()

        if not ADDR_RE.match(addr):
            messagebox.showerror(APP_NAME,
                "That doesn't look like a Bitcoin address.\n\nUse the address you want the "
                "block reward paid to (e.g. bc1q…). In solo mode the address IS your login.")
            return
        if not port.isdigit():
            messagebox.showerror(APP_NAME, "Port must be a number (e.g. 3335).")
            return

        miner = find_user_miner() or self.engine_path or find_local_engine()
        if not miner or not os.path.isfile(miner):
            if not self.engine_ready and not self.engine_error:
                messagebox.showinfo(APP_NAME,
                    "The mining engine is still downloading (one-time, ~18 MB). "
                    "Give it a moment, then click Start again.")
            else:
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

        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_lbl.config(text="● connecting…", fg=ORANGE)

    def _read_output(self):
        try:
            for line in self.proc.stdout:
                self.q.put(line.rstrip("\n"))
        except Exception:
            pass
        self.q.put(("__EXIT__", self.proc.poll() if self.proc else None))

    def stop(self, user=True):
        if self.proc:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=4)
                except Exception:
                    self.proc.kill()
            except Exception:
                pass
        self.proc = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_lbl.config(text="● stopped", fg=MUTED)
        self.hr_lbl.config(text="—")
        if user:
            self._logln("— stopped —", MUTED)

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
        self.root.after(200, self._pump)

    def _handle_event(self, item):
        kind = item[0]
        if kind == "__EXIT__":
            if self.proc is not None:
                self._logln("— miner exited (code %s) —" % item[1], RED)
                self.stop(user=False)
        elif kind == "__ENG__":
            name, why = item[1], item[2]
            tier = self._tier_from_name(name)
            self.engine_lbl.config(
                text="Engine: %s%s · %s" % (name, (" (" + tier + ")") if tier else "", why))
            self._logln("Engine ready: %s%s [%s]"
                        % (name, (" — " + tier) if tier else "", why), MUTED)
        elif kind == "__ENGMSG__":
            self.engine_lbl.config(text=item[1])
            self._logln(item[1], MUTED)
        elif kind == "__ENGERR__":
            self.engine_lbl.config(text="Engine: unavailable — press Start for help", fg=RED)
            self._logln("Engine could not be prepared: %s" % item[1], RED)
        elif kind == "__SAVECFG__":
            self._save_cfg()

    def _handle_line(self, line):
        low = line.lower()
        color = None
        if "accepted" in low or "yes!" in low:
            color = GREEN
            self.status_lbl.config(text="● mining", fg=GREEN)
        elif "rejected" in low or "booo" in low:
            color = RED
        elif "stratum" in low or "connect" in low:
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


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    if "--minetest" in sys.argv:
        i = sys.argv.index("--minetest")
        rest = sys.argv[i + 1:]
        secs = int(rest[0]) if len(rest) > 0 and rest[0].isdigit() else 90
        addr = rest[1] if len(rest) > 1 else ""
        thr = int(rest[2]) if len(rest) > 2 and rest[2].isdigit() else 2
        _minetest(secs, addr, thr)
        return
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:
        pass
    MinerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
