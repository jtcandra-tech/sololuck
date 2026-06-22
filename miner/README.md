# SoloLuck Miner

A simple Windows app that mines Bitcoin (CPU) to **sololuck.io**.
It wraps the proven **cpuminer-opt** engine and gives you a clean Start/Stop window
with live hashrate and share counts.

> A PC's hashrate is tiny next to an ASIC, so this is a **lottery ticket** — which is
> exactly the point of a solo pool. If your CPU solves a block, the whole reward is paid
> straight to *your* address on-chain (minus the pool's flat 2% fee).

---

## Use it (2 steps)

The cpuminer-opt engine is **bundled inside the app** — there's nothing to download and
nothing to configure. On first launch the app detects your CPU and automatically picks
the fastest compatible engine build (AVX-512 / AVX2 / SHA / SSE4.2 / SSE2).

### 1. Run it
Grab **`SoloLuckMiner.exe`** from https://sololuck.io and run it. (No install.)

### 2. Fill in and Start
- **BTC payout address** — where the block reward goes (your wallet). In solo mode the
  address *is* your login.
- **Worker name** — anything (e.g. `pc`).
- **Pool host** `sololuck.io`, **Port** stays on `3335` (Nano) for CPUs.
- Click **Start Mining**. The selected engine shows under the stats.

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

## Build it yourself (optional)
The repo ships *without* binaries to stay lean. `build.bat` fetches the engine and bundles it:

1. Install Python 3 (tkinter ships with it on Windows).
2. Double-click **`build.bat`** — it runs `fetch-engine.ps1` to download the latest
   cpuminer-opt Windows builds into `.\engine\`, then PyInstaller embeds them into a single
   `dist\SoloLuckMiner.exe`.

To just run the source: `python sololuck_miner.py` after `powershell -ExecutionPolicy Bypass
-File fetch-engine.ps1` (so `.\engine\` exists). Advanced users can instead drop their own
`cpuminer-opt.exe` next to the app — it overrides the bundled engine.

---

## Antivirus / Windows Defender (important)
**Every CPU miner trips antivirus false-positives** — it's the bundled cpuminer-opt engine
(it only hashes), not this GUI. If Defender quarantines it you'll see a clear in-app message;
if mining won't start:
1. Windows Security → **Virus & threat protection**.
2. **Protection history** → **Allow / Restore** any "SoloLuck" or "cpuminer" item.
3. Add an **Exclusion (Folder)** for the `SoloLuckMiner-engine` folder next to the app
   (the app tells you the exact path).
4. Reopen the app and click **Start Mining**.

The app extracts the engine to that stable folder on purpose, so a single exclusion sticks.

## Notes
- A PC's hashrate is tiny vs ASICs — treat this as a lottery ticket, fitting for a solo pool.
- This app is pure Python standard library (tkinter) — it has no network code of its own; it
  only launches cpuminer-opt and reads its output. Your address never leaves your machine
  except as the stratum login to the pool you choose.
- The bundled **cpuminer-opt** engine is GPLv2 software by Jay D Dee —
  source: https://github.com/JayDDee/cpuminer-opt (its licence ships in the app's `engine`
  folder as `cpuminer-opt-LICENSE.txt`). This GUI is open-source; mine to any ckpool-style
  pool by changing the host/port.

Website: https://sololuck.io · Channel: https://t.me/SoloLuckPool
