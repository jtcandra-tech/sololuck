# SoloLuck Miner

A simple Windows app that mines Bitcoin (CPU) to **sololuck.io** — a clean Start/Stop
window with live hashrate and share counts. It drives the proven **cpuminer-opt** engine.

> A PC's hashrate is tiny next to an ASIC, so this is a **long shot** — which is
> exactly the point of a solo pool. If your CPU solves a block, the whole reward is paid
> straight to *your* address on-chain — the whole reward, 0% pool fee.

This is a **clean wrapper**: it has no mining code inside it. On first run it detects your
CPU and **downloads the matching build from the pinned cpuminer-opt release** (Jay D Dee's,
GPLv2) into a folder next to the app, **verifies every file against a SHA-256 manifest
baked into the app** (anything that doesn't match is quarantined, never run), then runs it.
Nothing to install. If a too-new build ever crashes on an older CPU, the app automatically
retries with the universal SSE2 build.

---

## Use it (2 steps)

### 1. Run it
Grab the latest **`SoloLuckMiner-v1.4.0.exe`** from https://sololuck.io and run it. On
first launch it downloads the engine (~18 MB, one time) — you'll see "Downloading the
mining engine…".

### 2. Fill in and Start
- **BTC payout address** — where the block reward goes (your wallet). In solo mode the
  address *is* your login. The app checks it live: a green **✓** means the checksum is
  a real Bitcoin address (legacy, SegWit and Taproot all fine).
- **Worker name** — anything (e.g. `pc`).
- The pool endpoint is **fixed** to `sololuck.io:3335` (Nano tier — its low starting
  difficulty is what makes CPU shares register fast).
- Pick a **CPU load** (starts gentle at 25%; 100% is opt-in and will slow your PC).
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

## macOS (Intel & Apple Silicon)
There's no prebuilt macOS engine to bundle (and no notarized `.app` yet), so on a Mac the
miner is built from source by a one-line installer. In **Terminal**:

```sh
curl -fsSL https://sololuck.io/mac-miner.sh | bash
```

It asks for your BTC address, installs build tools (Xcode Command Line Tools + Homebrew
deps), compiles **cpuminer-opt** (falling back to **pooler/cpuminer**), and mines to
`stratum.sololuck.io:3335` (the Nano tier). `Ctrl-C` to stop. The script is
[`mac-miner.sh`](mac-miner.sh) in this repo.

Manual: `brew install autoconf automake libtool pkg-config curl jansson`, build
[cpuminer-opt](https://github.com/JayDDee/cpuminer-opt), then
`cpuminer -a sha256d -o stratum+tcp://stratum.sololuck.io:3335 -u YOURADDR.mac -p x`.

---

## Notes
- A PC's hashrate is tiny vs ASICs — treat this as a lottery ticket, fitting for a solo pool.
- Pure Python standard library (tkinter + urllib). Its only network use is downloading the
  engine from GitHub and, while mining, the stratum connection to the pool you choose. Your
  address never leaves your machine except as the stratum login.
- The **cpuminer-opt** engine is GPLv2 by Jay D Dee — https://github.com/JayDDee/cpuminer-opt.
  Open-source; mine to any ckpool-style pool by changing the host/port.

---

## About SoloLuck (the pool)
[SoloLuck](https://sololuck.io) is a public **true-solo** Bitcoin pool, hosted in
**Jakarta, Indonesia** for low latency across Asia. You mine with your **own** BTC address as
the username; if you solve a block, the network pays the full reward straight to you. The only
fee is **0% — finders keepers**; solve a block and the whole reward is yours. Non-custodial —
no account, no KYC.

- **Stratum host:** `stratum.sololuck.io`
  - `:3335` Nano (diff 1, CPUs/nerdminers) · `:3333` Lite (Bitaxe) · `:8081` Standard (ASIC) ·
    `:4334` Pro · `:3334` TLS (encrypted)
- **Coinbase tag:** `/sololuck.io/`
- **Setup / connect:** https://sololuck.io/setup
- **Verify the claims yourself:** https://sololuck.io/verify
- **Compare vs other solo pools:** https://sololuck.io/compare

Website: https://sololuck.io · Channel: https://t.me/SoloLuckPool
