#!/usr/bin/env python3
"""
SoloLuck Miner — a simple Start/Stop GUI that wraps the cpuminer-opt CPU engine
and points it at the SoloLuck solo Bitcoin pool (sololuck.io).

A PC's hashrate is tiny next to an ASIC, so this is a lottery ticket — which is
exactly the point of a solo pool. If your CPU happens to solve a block, the whole
reward is paid straight to your address on-chain (minus the pool's flat 2% fee).

Setup: keep a cpuminer-opt build named cpuminer-opt.exe (or cpuminer-avx2.exe /
cpuminer-zen3.exe / etc.) in the SAME folder as this app — it auto-detects common
names. Then fill in your BTC address and click Start Mining.

Pure standard-library (tkinter + subprocess) — no third-party Python deps.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "SoloLuck Miner"
DEFAULT_HOST = "sololuck.io"
DEFAULT_PORT = "3335"   # Nano tier — diff 1, tuned for CPUs so shares register fast
ALGO = "sha256d"   # Bitcoin
# cpuminer-opt executable names we recognise, in the app's own folder.
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


CFG_PATH = os.path.join(app_dir(), "sololuck_miner.cfg")


def find_miner():
    """Return the path to a recognised cpuminer-opt build in the app folder, or None."""
    d = app_dir()
    for name in MINER_NAMES:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    # last resort: any file that looks like a cpuminer build
    try:
        for f in os.listdir(d):
            low = f.lower()
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
        root.title(APP_NAME)
        root.configure(bg=BG)
        root.minsize(560, 560)
        self._build_ui()
        self._load_cfg()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self._pump)

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

        logf = tk.Frame(self.root, bg=BG)
        logf.pack(fill="both", expand=True, padx=14, pady=(8, 14))
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
        except Exception:
            pass

    def _save_cfg(self):
        try:
            with open(CFG_PATH, "w") as f:
                json.dump({"addr": self.addr_var.get().strip(),
                           "worker": self.worker_var.get().strip(),
                           "host": self.host_var.get().strip(),
                           "port": self.port_var.get().strip(),
                           "threads": self.threads_var.get().strip()}, f)
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
            messagebox.showerror(APP_NAME, "Port must be a number (e.g. 3333).")
            return
        miner = find_miner()
        if not miner:
            messagebox.showerror(APP_NAME,
                "Couldn't find a cpuminer-opt build.\n\nDownload one from\n"
                "https://github.com/JayDDee/cpuminer-opt/releases\n\n"
                "and put it in the SAME folder as this app (e.g. cpuminer-opt.exe "
                "or cpuminer-avx2.exe).")
            return

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
                if isinstance(item, tuple) and item and item[0] == "__EXIT__":
                    if self.proc is not None:
                        self._logln("— miner exited (code %s) —" % item[1], RED)
                        self.stop(user=False)
                    continue
                self._handle_line(item)
        except queue.Empty:
            pass
        self.root.after(200, self._pump)

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
                self.hashrate = "%s %sH/s" % (hm.group(1), hm.group(2) or "")
                self.hr_lbl.config(text=self.hashrate)

        self._logln(line, color)

    def on_close(self):
        self.stop(user=False)
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:
        pass
    MinerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
