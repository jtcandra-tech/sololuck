# SoloLuck Miner

A simple Windows app that mines Bitcoin (CPU) to **sololuck.io** — a clean Start/Stop
window with live hashrate and share counts. It drives the proven **cpuminer-opt** engine.

> A PC's hashrate is tiny next to an ASIC, so this is a **lottery ticket** — which is
> exactly the point of a solo pool. If your CPU solves a block, the whole reward is paid
> straight to *your* address on-chain (minus the pool's flat 2% fee).

This is a **clean wrapper**: it has no mining code inside it. On first run it detects your
CPU and **downloads the matching cpuminer-opt build** (Jay D Dee's, GPLv2) into a folder
next to the app, then runs it. Nothing to install.

---

## Use it (2 steps)

### 1. Run it
Grab **`SoloLuckMiner.exe`** from https://sololuck.io and run it. On first launch it
downloads the engine (~18 MB, one time) — you'll see "Downloading the mining engine…".

### 2. Fill in and Start
- **BTC payout address** — where the block reward goes (your wallet). In solo mode the
  address *is* your login.
- **Worker name** — anything (e.g. `pc`).
- **Pool host** `sololuck.io`, **Port** stays on `3335` (Nano) for CPUs.
- Click **Start Mining**. The selected engine shows under the stats.

---

## Antivirus / Windows Defender (important)
The **app itself** is clean and won't be flagged. But **the downloaded cpuminer engine
will trip antivirus** — every CPU miner does (it's the engine, not this app; it only hashes).
If the download is blocked or mining won't start:
1. Windows Security → **Virus & threat protection**.
2. **Protection history** → **Allow / Restore** any "SoloLuck" or "cpuminer" item.
3. Add an **Exclusion (Folder)** for the `SoloLuckMiner-engine` folder next to the app
   (the app shows the exact path).
4. Reopen the app (it re-downloads if needed) and click **Start Mining**.

One exclusion sticks because the engine lives in that one stable folder.

---

## Ports (difficulty tiers)
The port only sets your *starting* difficulty; vardiff tunes it automatically.
- `3335` — **Nano (default)** — difficulty 1, tuned for CPUs so your shares register fast
- `3333` — Lite (Bitaxe / small ASICs) · `8081` — Standard · `4334` — Pro · `3334` — TLS

For CPU mining, **stick with 3335 (Nano)** — higher tiers can take days for a CPU to submit
a single share.

---

## Build it yourself / offline
- **Build the .exe:** install Python 3, double-click **`build.bat`** → `dist\SoloLuckMiner.exe`.
  (No engine is bundled; it's fetched at runtime.)
- **Run the source:** `python sololuck_miner.py`.
- **Offline / skip the download:** drop your own `cpuminer-opt.exe` (from
  https://github.com/JayDDee/cpuminer-opt/releases) next to the app — it's used instead.

---

## Notes
- A PC's hashrate is tiny vs ASICs — treat this as a lottery ticket, fitting for a solo pool.
- Pure Python standard library (tkinter + urllib). Its only network use is downloading the
  engine from GitHub and, while mining, the stratum connection to the pool you choose. Your
  address never leaves your machine except as the stratum login.
- The **cpuminer-opt** engine is GPLv2 by Jay D Dee — https://github.com/JayDDee/cpuminer-opt.
  Open-source; mine to any ckpool-style pool by changing the host/port.

Website: https://sololuck.io · Channel: https://t.me/SoloLuckPool
