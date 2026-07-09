# SoloLuck Miner — changelog

## v1.6.0 — 2026-07-10
- Fix: ticking "Allow 100% CPU" now actually runs at 100%. Before, if the
  slider had snapped back to the 80% cap, checking the box left it at 80% — so
  a "100%"-checked miner kept hashing at 80%. Ticking the box now sets 100%.
- Auto-install updates: when a newer version is found the app downloads and
  SHA-256-verifies it in the background, then installs automatically the moment
  you are not mining (or immediately if idle). Mining is never interrupted —
  a verified update waits and applies when you Stop, or on demand.

## v1.5.0 — 2026-07-10
- Auto-detected CPU panel: your processor name, core/thread count and the exact
  cpuminer SIMD path it will use for mining (e.g. "AVX-512 + SHA" on modern AMD,
  "AVX2 + SHA" on 12th-gen Intel) — shown the moment the app opens.
- Live per-core load meter: a bar per logical core so you can see exactly which
  cores the miner is driving, with an "N of M cores active" count.
- CPU-load slider now shows a green "recommended" threshold (up to 80%) that
  turns amber once you opt into higher, hotter loads.
- Built-in auto-update: the app checks sololuck.io on launch and, if a newer
  version exists, shows a banner. Updating downloads the new versioned build,
  verifies its SHA-256 against the site manifest (fail-closed), and relaunches.
- UI polish throughout.

## v1.4.0 — 2026-07-10
- Real Bitcoin address verification with a live indicator: base58check for
  legacy/P2SH and BIP-173/BIP-350 bech32(m) for bc1q/bc1p, checked as you
  type — a green ✓ names the address kind; a red ✗ tells you why it's wrong
  (typo/checksum, testnet, even a pasted Ethereum address). Start refuses
  anything that fails the checksum, because on a solo pool a mistyped payout
  address means an unclaimable block.
- Pool host and port are now fixed to sololuck.io:3335 (Nano tier). The app
  has one job; removing the editable fields removes the ways to break it.
- The app shows its version with a "What's new" link to sololuck.io/changelog.
- Release files are versioned: SoloLuckMiner-v1.4.0.exe / -src.zip (old
  unversioned URLs redirect).
- UI polish: card-style form with focus rings on inputs, bordered stat tiles,
  button hover states, tidier spacing.

## v1.3.0 — 2026-07-10
- CPU load is now a slider, not a thread count. It starts gentle at 25% of
  your cores and tops out at 80%; the thread math is done for you
  ("25% · 3 of 12 threads").
- Mining at 100% CPU is opt-in via an explicit checkbox with an honest
  warning: full load makes the PC noticeably slower. Most people should mine
  throttled — the lottery odds are the same, per-hash, either way.
- Old configs with a saved thread count migrate onto the slider automatically.
- Fail-closed hardening: if the "run your own engine?" dialog cannot be shown
  (headless/display failure), the unverified engine is refused.

## v1.2.0 — 2026-07-10
- SECURITY FIX: the GUI now enforces the pinned-engine rules end-to-end. In
  v1.1.0 only the headless test paths verified engines; the window itself would
  run any cpuminer*.exe found next to the app without checking the SHA-256
  manifest or asking. Now every path verifies, and an unverified user-supplied
  engine runs only after an explicit confirmation (remembered for the session).
- Honest connection status: "Stratum connection failed / interrupted / retry"
  lines now show "reconnecting" (previously any line containing
  "stratum"/"connect" — including failures — turned the status green "mining").
- Crash exits are explained in plain words (illegal instruction, missing DLL,
  …) and a build that crashes at launch on an older CPU automatically retries
  once with the universal SSE2 build instead of dying with a cryptic code.
- AVX-only CPUs (Sandy/Ivy Bridge era) now get the faster cpuminer-avx build
  instead of falling back to sse42.
- Stop no longer freezes the window (process reaping moved off the UI thread),
  and a quick Stop→Start can no longer be ended by the previous run's exit.
- If the one-time engine download fails, Start offers Retry — no app restart.
- Polish: high-DPI awareness (crisp text on modern displays), window icon,
  session timer, a link to your live pool stats after the first accepted
  share, log scrollbar no longer overlaps the text, port range validation,
  thread hint shows your real core count.

## v1.1.0 — 2026-07-02
- SECURITY: the cpuminer-opt engine is now **pinned to v26.1** with an exact
  download URL and a SHA-256 manifest baked into the app. The engine archive is
  verified before extraction, every extracted file is re-verified on disk, and a
  cached engine is re-verified on every launch. Any mismatch is quarantined and
  never executed (fail closed). No 'latest', no mirror fallback.
- A user-supplied engine runs only if it matches the manifest or the user
  explicitly confirms their own file; headless refuses.
- Engine version shown in the window title; full audit trail in engine-verify.log.
- Copy fixes: 0% pool fee (was a stale '2%'); 'long shot' instead of 'lottery ticket'.
- Antivirus guidance no longer suggests disabling protection; it explains
  verification and source-building first.

## v1.0.0 — 2026-06 (prior)
- Initial clean-wrapper GUI (no bundled engine); SHA-256 published for the .exe.
