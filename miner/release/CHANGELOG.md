# SoloLuck Miner — changelog

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
