# SoloLuck Miner

A simple Windows app that mines Bitcoin (CPU) to **sololuck.io**.
It wraps the proven **cpuminer-opt** engine and gives you a clean Start/Stop window
with live hashrate and share counts.

> A PC's hashrate is tiny next to an ASIC, so this is a **lottery ticket** — which is
> exactly the point of a solo pool. If your CPU solves a block, the whole reward is paid
> straight to *your* address on-chain (minus the pool's flat 2% fee).

---

## Setup (3 steps)

### 1. Get the engine
Download cpuminer-opt for Windows (v25.5+):
https://github.com/JayDDee/cpuminer-opt/releases

Pick the build for your CPU:
- **Most modern CPUs:** `cpuminer-avx2.exe`
- **AMD Ryzen 5000 / 7000:** `cpuminer-zen3.exe` / `cpuminer-zen4.exe`
- **Very old CPU:** `cpuminer-sse2.exe`

Rename it to `cpuminer-opt.exe` (or leave the name — the app auto-detects common names).

### 2. Run the app
Two options:
- **Quick:** `python sololuck_miner.py`  (needs Python 3 — tkinter ships with it on Windows)
- **Build an .exe:** double-click `build.bat`, then run `dist\SoloLuckMiner.exe`

**Keep `cpuminer-opt.exe` in the SAME folder as the app.**

### 3. Fill in and Start
- **BTC payout address** — where the block reward goes (your wallet). In solo mode the
  address *is* your login.
- **Worker name** — anything (e.g. `pc`).
- **Pool host** `sololuck.io`, **Port** = your stratum port.
- Click **Start Mining**.

---

## Ports (difficulty tiers)
The port only sets your *starting* difficulty; vardiff tunes it automatically.
- `3335` — **Nano (default)** — difficulty 1, tuned for CPUs so your shares register fast
- `3333` — Lite (Bitaxe / small ASICs)
- `8081` — Standard
- `4334` — Pro
- `3334` — TLS / SSL (encrypted; needs a TLS-capable miner)

For CPU mining, **stick with 3335 (Nano)** — at the higher tiers a CPU can take days to
submit a single share, so it'd never show up.

---

## Notes
- A PC's hashrate is tiny vs ASICs — treat this as a lottery ticket, fitting for a solo pool.
- The default port `3333` is correct for almost everyone.
- **Antivirus may flag any miner as a false positive** — that's expected for mining software
  (it's the bundled `cpuminer-opt.exe`, not this GUI). You may need to allow it.
- This app is pure Python standard library (tkinter) — it has no network code of its own; it
  only launches `cpuminer-opt` and reads its output. Your address never leaves your machine
  except as the stratum login to the pool you choose.
- Open-source. Mine to any ckpool-style pool by changing the host/port.

Website: https://sololuck.io · Channel: https://t.me/SoloLuckPool
