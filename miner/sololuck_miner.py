#!/usr/bin/env python3
"""
SoloLuck Miner — a simple Start/Stop GUI that wraps the cpuminer-opt CPU engine
and points it at the SoloLuck solo Bitcoin pool (sololuck.io).

A PC's hashrate is tiny next to an ASIC, so this is a lottery ticket — which is
exactly the point of a solo pool. If your CPU happens to solve a block, the whole
reward is paid straight to your address on-chain (minus the pool's flat 2% fee).

The cpuminer-opt engine is BUNDLED inside this app — there is nothing to download.
On first launch the app detects your CPU and automatically picks the fastest
compatible cpuminer-opt build (AVX-512 / AVX2 / SHA / SSE4.2 / SSE2), falling back
safely if a faster build isn't supported. Advanced users can still drop their own
cpuminer build (e.g. cpuminer-opt.exe / cpuminer-zen4.exe) next to this app and it
will be used instead.

cpuminer-opt is GPLv2 software by Jay D Dee — source: https://github.com/JayDDee/cpuminer-opt
(its licence ships alongside the bundled engine). This GUI is pure Python standard
library (tkinter + subprocess) — no third-party Python deps, and no network code of
its own; it only launches cpuminer-opt and reads its output.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "SoloLuck Miner"
DEFAULT_HOST = "sololuck.io"
DEFAULT_PORT = "3335"   # Nano tier — diff 1, tuned for CPUs so shares register fast
ALGO = "sha256d"   # Bitcoin
ENGINE_SUBDIR = "engine"   # where the bundled cpuminer-opt builds live
# cpuminer-opt executable names we recognise as a USER-SUPPLIED override in the
# app's own folder (takes priority over the bundled engine).
MINER_NAMES = [
    "cpuminer-opt.exe", "cpuminer.exe",
    "cpuminer-avx2.exe", "cpuminer-avx512.exe", "cpuminer-avx.exe",
    "cpuminer-zen.exe", "cpuminer-zen3.exe", "cpuminer-zen4.exe", "cpuminer-zen5.exe",
    "cpuminer-sse2.exe", "cpuminer-sse42.exe", "cpuminer-aes-sse42.exe",
    # non-Windows (dev / Linux) fallbacks
    "cpuminer-opt", "cpuminer",
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


def engine_dir():
    """Folder holding the BUNDLED cpuminer-opt builds. For a PyInstaller --onefile
    .exe the engines are unpacked under sys._MEIPASS/engine; when run as a script
    they sit in ./engine next to the source. Falls back to the app folder."""
    base = getattr(sys, "_MEIPASS", None) or app_dir()
    d = os.path.join(base, ENGINE_SUBDIR)
    if os.path.isdir(d):
        return d
    d2 = os.path.join(app_dir(), ENGINE_SUBDIR)
    return d2 if os.path.isdir(d2) else app_dir()


CFG_PATH = os.path.join(app_dir(), "sololuck_miner.cfg")


# Windows PF_* ids for kernel32!IsProcessorFeaturePresent (native, no deps).
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
    """Run the x86-64 CPUID instruction via a tiny ctypes shellcode stub and return
    (eax, ebx, ecx, edx). Windows 64-bit only; raises on anything else."""
    if os.name != "nt":
        raise OSError("cpuid: Windows only")
    import ctypes
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise OSError("cpuid: 64-bit only")
    # Windows x64 ABI: leaf->ecx, subleaf->edx, out ptr->r8.
    code = bytes((
        0x53,                    # push rbx
        0x4D, 0x89, 0xC1,        # mov r9, r8      ; r9 = out ptr
        0x89, 0xC8,              # mov eax, ecx    ; eax = leaf
        0x89, 0xD1,              # mov ecx, edx    ; ecx = subleaf
        0x0F, 0xA2,              # cpuid
        0x41, 0x89, 0x01,        # mov [r9],    eax
        0x41, 0x89, 0x59, 0x04,  # mov [r9+4],  ebx
        0x41, 0x89, 0x49, 0x08,  # mov [r9+8],  ecx
        0x41, 0x89, 0x51, 0x0C,  # mov [r9+12], edx
        0x5B,                    # pop rbx
        0xC3,                    # ret
    ))
    k = ctypes.windll.kernel32
    k.VirtualAlloc.restype = ctypes.c_void_p
    k.VirtualAlloc.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong, ctypes.c_ulong)
    addr = k.VirtualAlloc(None, len(code), 0x3000, 0x40)  # MEM_COMMIT|RESERVE, EXECUTE_READWRITE
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
        k.VirtualFree(addr, 0, 0x8000)  # MEM_RELEASE


def cpu_features():
    """Detect the instruction sets that decide which cpuminer-opt build to run.
    AVX-family flags come from the OS (IsProcessorFeaturePresent — it also confirms
    the OS will actually preserve those registers); AES / SHA-NI / VAES come from
    CPUID (the OS API has no flag for them). Everything fails safe to False."""
    f = {"AVX512F": _has("AVX512F"), "AVX2": _has("AVX2"),
         "AVX": _has("AVX"), "SSE42": _has("SSE42"),
         "AES": False, "SHA": False, "VAES": False}
    try:
        _, _, ecx1, _ = _cpuid(1)
        _, ebx7, ecx7, _ = _cpuid(7, 0)
        f["AES"] = bool(ecx1 & (1 << 25))
        f["SHA"] = bool(ebx7 & (1 << 29))
        f["VAES"] = bool(ecx7 & (1 << 9))
        # corroborate the OS-reported flags if CPUID is available
        if not f["SSE42"]:
            f["SSE42"] = bool(ecx1 & (1 << 20))
    except Exception:
        pass
    return f


def cpu_signature():
    """A fingerprint of the CPU so a portable .exe re-selects its engine if copied
    to a different machine."""
    import platform
    fe = cpu_features()
    flags = "".join("1" if fe[k] else "0" for k in
                    ("AVX512F", "AVX2", "AVX", "SSE42", "AES", "SHA", "VAES"))
    try:
        brand = platform.processor() or ""
    except Exception:
        brand = ""
    return flags + "|" + brand


def _engine_candidates():
    """Bundled cpuminer-opt build names, best→safest for THIS CPU. Selection is fully
    deterministic from detected features (no launch probing) and always ends at the
    universal sse2 build, so a present file is always found."""
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


def select_engine():
    """Pick the best bundled cpuminer-opt build present for this CPU. Deterministic,
    offline. Returns a path, or None if no bundled engine is present."""
    d = engine_dir()
    for n in _engine_candidates():
        p = os.path.join(d, n)
        if os.path.isfile(p):
            return p
    # nothing matched (unexpected) — fall back to any bundled cpuminer build
    try:
        for fn in sorted(os.listdir(d)):
            low = fn.lower()
            if low.startswith("cpuminer") and low.endswith(".exe"):
                return os.path.join(d, fn)
    except OSError:
        pass
    return None


def _engine_stage_base():
    """A STABLE, writable folder to copy the engine into (so its path doesn't change
    every launch like the PyInstaller temp dir, and the user can add ONE antivirus
    exclusion that sticks). Tries next-to-the-exe first, then LOCALAPPDATA, then TEMP."""
    cands = [app_dir(),
             os.environ.get("LOCALAPPDATA", ""),
             os.environ.get("TEMP", "")]
    for base in cands:
        if not base:
            continue
        d = os.path.join(base, "SoloLuckMiner-engine")
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, ".wtest")
            with open(t, "w"):
                pass
            os.remove(t)
            return d
        except Exception:
            continue
    return None


def stage_engine():
    """Resolve the engine to run. For the frozen .exe, copy the selected build + its
    runtime DLLs out of the volatile _MEIPASS temp dir into a stable folder and run
    from THERE — and prefer a copy that's already staged, so once the user excludes
    that folder in their antivirus it keeps working even if AV re-quarantines the
    temp copy on every launch. Falls back to running in place if staging isn't
    possible. Returns a path or None."""
    if not getattr(sys, "frozen", False):
        return select_engine()  # dev/script mode: run in place
    base = _engine_stage_base()
    if not base:
        return select_engine()
    # 1) a preferred build already staged (survives AV quarantine of the temp copy)
    for n in _engine_candidates():
        p = os.path.join(base, n)
        if os.path.isfile(p):
            return p
    # 2) copy the selected build + all DLLs out of the bundle
    src = select_engine()
    if not src:
        return None
    try:
        import shutil
        srcdir = os.path.dirname(src)
        names = [os.path.basename(src)] + [f for f in os.listdir(srcdir) if f.lower().endswith(".dll")]
        for name in names:
            d = os.path.join(base, name)
            if not os.path.isfile(d):
                shutil.copy2(os.path.join(srcdir, name), d)
        dest = os.path.join(base, os.path.basename(src))
        return dest if os.path.isfile(dest) else src
    except Exception:
        return src  # last resort: run straight from _MEIPASS


def _engine_missing_msg():
    """Actionable message for when the engine can't be launched — almost always an
    antivirus false-positive that quarantined the bundled cpuminer engine."""
    folder = _engine_stage_base() or app_dir()
    return ("The mining engine was blocked or removed by your antivirus.\n\n"
            "Windows Defender either quarantined it (WinError 2) or blocked it outright as "
            "a virus/PUA (WinError 225). EVERY CPU miner trips this false-positive — it's the "
            "bundled cpuminer-opt engine, not this app, and it does nothing but hash.\n\n"
            "Fix it once:\n"
            "  1. Open Windows Security  →  Virus & threat protection.\n"
            "  2. Under \"Protection history\", Allow / Restore any SoloLuck or cpuminer item.\n"
            "  3. Add an Exclusion (Folder) for:\n"
            "       %s\n"
            "  4. Reopen SoloLuck Miner and click Start Mining.\n\n"
            "Advanced: you can instead drop your own cpuminer-opt.exe next to this app." % folder)


def find_miner():
    """A USER-SUPPLIED cpuminer-opt build dropped in the app folder (overrides the
    bundled engine). Returns its path, or None."""
    d = app_dir()
    for name in MINER_NAMES:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    # last resort: any file that looks like a cpuminer build, but never the
    # bundled engine subfolder
    try:
        for f in os.listdir(d):
            low = f.lower()
            if low == ENGINE_SUBDIR:
                continue
            if low.startswith("cpuminer") and (low.endswith(".exe") or "." not in low):
                return os.path.join(d, f)
    except OSError:
        pass
    return None


class MinerApp:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.reader = None
        self.q = queue.Queue()
        self.accepted = 0
        self.rejected = 0
        self.hashrate = "—"
        self.engine_path = None          # resolved cpuminer build to run
        self._cfg_engine = ""            # cached engine basename (from cfg)
        self._cfg_sig = ""               # cached CPU signature (from cfg)
        self._start_ts = 0               # last miner launch time (crash guard)
        self._saw_hash = False           # did the current run ever report a hashrate
        self._auto_retry = False         # one auto re-select after a fast crash
        root.title(APP_NAME)
        root.configure(bg=BG)
        root.minsize(560, 580)
        self._build_ui()
        self._load_cfg()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self._pump)
        # resolve the bundled engine in the background so the UI stays responsive
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

        self.engine_lbl = tk.Label(self.root, text="Engine: detecting your CPU…",
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
        # cap the log so it never grows unbounded
        if int(self.log.index("end-1c").split(".")[0]) > 600:
            self.log.delete("1.0", "200.0")
        self.log.configure(state="disabled")

    # ---------- engine resolution (runs off the UI thread) ----------
    def _init_engine(self):
        # 1) a user-supplied build in the app folder always wins
        over = find_miner()
        if over:
            self.engine_path = over
            self.q.put(("__ENG__", os.path.basename(over), "your own build"))
            return
        # 2) detect the best build and stage it to a stable, AV-excludable folder
        self.q.put(("__ENGMSG__", "Engine: preparing the best build for your CPU…"))
        p = stage_engine()
        if p and os.path.isfile(p):
            self.engine_path = p
            self._cfg_engine = os.path.basename(p)
            self._cfg_sig = cpu_signature()
            self.q.put(("__ENG__", self._cfg_engine, "bundled, auto-selected"))
            self.q.put(("__SAVECFG__", None))
        else:
            self.q.put(("__ENGMSG__", "Engine: blocked by antivirus? You'll get steps when you press Start."))

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
        # the bundled engine is normally resolved at launch; if selection is still
        # running (or this is a dev run), resolve/stage it now.
        miner = self.engine_path or find_miner() or stage_engine()
        if not miner or not os.path.isfile(miner):
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
            # WinError 2 = engine missing (quarantined); 225 = blocked outright as a
            # virus/PUA; 226 = blocked, comment. All are antivirus interference.
            self.proc = None
            self.engine_path = None
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

    # ---------- output parsing ----------
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
                code = item[1]
                crashed_fast = (time.time() - self._start_ts) < 6 and not self._saw_hash
                self._logln("— miner exited (code %s) —" % code, RED)
                self.stop(user=False)
                # a fast crash with no output usually means the cached build doesn't
                # run on this CPU (e.g. the .exe was copied here) — reselect once.
                if crashed_fast and not self._auto_retry and not find_miner():
                    self._auto_retry = True
                    self._logln("Re-detecting a compatible engine and retrying…", ORANGE)
                    self._cfg_engine = ""
                    self.engine_path = None
                    threading.Thread(target=self._reselect_and_restart, daemon=True).start()
        elif kind == "__ENG__":
            name, why = item[1], item[2]
            tier = self._tier_from_name(name)
            self.engine_lbl.config(
                text="Engine: %s%s · %s" % (name, (" (" + tier + ")") if tier else "", why))
            self._logln("Engine ready: %s%s [%s]"
                        % (name, (" — " + tier) if tier else "", why), MUTED)
        elif kind == "__ENGMSG__":
            self.engine_lbl.config(text=item[1])
        elif kind == "__SAVECFG__":
            self._save_cfg()
        elif kind == "__RESTART__":
            # re-enter mining on the UI thread after a successful engine re-select
            if not self.proc:
                self.start()

    def _reselect_and_restart(self):
        p = stage_engine()
        if p and os.path.isfile(p):
            self.engine_path = p
            self._cfg_engine = os.path.basename(p)
            self._cfg_sig = cpu_signature()
            self.q.put(("__ENG__", self._cfg_engine, "bundled, re-selected"))
            self.q.put(("__SAVECFG__", None))
            self.q.put(("__RESTART__", None))

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

        # hashrate: prefer summary lines (total), ignore per-thread "cpu #" lines
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
    """Headless build-validation: resolve the bundled engine and run it with -V to
    confirm it launches (i.e. its bundled DLLs load) on this CPU. Writes the result
    to a file beside the .exe and exits. Triggered by `SoloLuckMiner.exe --selftest`;
    since the app is built --noconsole there is no stdout to read."""
    out = os.path.join(app_dir(), "sololuck_selftest.txt")
    lines = []
    def w(s):
        lines.append(str(s))
    try:
        w("engine_dir: %s" % engine_dir())
        w("features: %s" % cpu_features())
        w("signature: %s" % cpu_signature())
        eng = select_engine()
        w("selected: %s" % eng)
        if not eng:
            w("RESULT: FAIL (no bundled engine found)")
        else:
            d = os.path.dirname(eng)
            w("engine_files: %s" % sorted(os.listdir(d)))
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
            try:
                r = subprocess.run([eng, "-V"], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=20,
                                   creationflags=flags, text=True)
                w("engine -V exit: %s" % r.returncode)
                w("engine -V output (first 5 lines):")
                for ln in (r.stdout or "").splitlines()[:5]:
                    w("  " + ln)
                ok = r.returncode == 0 and "cpuminer" in (r.stdout or "").lower()
                w("RESULT: %s" % ("PASS — engine launches, DLLs load" if ok else "FAIL — engine did not run cleanly"))
            except Exception as e:
                w("engine launch error: %r" % e)
                w("RESULT: FAIL (engine could not be launched — missing DLL?)")
    except Exception as e:
        w("selftest error: %r" % e)
        w("RESULT: FAIL")
    try:
        with open(out, "w") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def _minetest(seconds, addr, threads):
    """Headless end-to-end test: resolve the bundled engine and actually mine to the
    live pool for `seconds`, exactly like the Start button. Writes the engine output
    + a PASS/FAIL to a file beside the .exe. Usage:
    SoloLuckMiner.exe --minetest <seconds> <btc-address> [threads]"""
    out = os.path.join(app_dir(), "sololuck_minetest.txt")
    log = []
    def w(s):
        log.append(str(s))
    eng = stage_engine()   # same path the Start button uses (stages out of _MEIPASS)
    w("engine: %s" % eng)
    if not eng:
        w("RESULT: FAIL (no engine)")
        open(out, "w").write("\n".join(log) + "\n"); return
    url = "stratum+tcp://%s:%s" % (DEFAULT_HOST, DEFAULT_PORT)
    user = "%s.%s" % (addr, "bundletest")
    cmd = [eng, "-a", ALGO, "-o", url, "-u", user, "-p", "x", "-t", str(threads)]
    w("cmd: %s" % " ".join(cmd))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
    captured = []
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, creationflags=flags)
    except Exception as e:
        w("launch error: %r" % e); w("RESULT: FAIL")
        open(out, "w").write("\n".join(log) + "\n"); return
    end = time.time() + seconds
    rdr = threading.Thread(target=lambda: [captured.append(l.rstrip()) for l in p.stdout], daemon=True)
    rdr.start()
    while time.time() < end and p.poll() is None:
        time.sleep(0.5)
    try:
        p.terminate(); p.wait(timeout=5)
    except Exception:
        try: p.kill()
        except Exception: pass
    blob = "\n".join(captured)
    low = blob.lower()
    connected = ("stratum" in low and ("connect" in low or "subscrib" in low or "authoriz" in low or "difficulty" in low))
    gotwork = "new work" in low or "new job" in low or "stratum requested work restart" in low
    hashing = "h/s" in low or "khash" in low or "hash rate" in low
    accepted = "accepted" in low or "yes!" in low
    w("--- engine output (last 30 lines) ---")
    for ln in captured[-30:]:
        w("  " + ln)
    w("--- signals: connected=%s gotwork=%s hashing=%s accepted=%s" % (connected, gotwork, hashing, accepted))
    ok = connected and (gotwork or hashing)
    w("RESULT: %s" % ("PASS — mining live to the pool" + (" (share accepted!)" if accepted else "")
                      if ok else "FAIL — did not establish mining"))
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
