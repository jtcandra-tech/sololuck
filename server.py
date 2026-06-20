#!/usr/bin/env python3
"""
SoloLuck : public-facing solo Bitcoin mining pool landing + stats.

Asia's community solo Bitcoin pool. Mine to your OWN address; if YOU strike a
block YOU keep the whole reward minus a flat 2% fee. Non-custodial — no account,
no KYC, we never hold your coins. Transparent: real hashrate, real odds, real
blocks, real fee.

PUBLIC / INTERNET-FACING. Bind 127.0.0.1:8201 only; nginx is wired in front of
this separately. Stdlib only — no external assets, no trackers, no cookies.

Data sources (read-only, already-public):
  - pool stats JSON
  - solved-block markers from the pool log (+ optional blocks file).

SANITIZATION (this is public):
  * NEVER expose any node internals (version/peers/mempool/sync/halving/
    retarget) -> we never call RPC and never read node state here.
  * NEVER expose the operator payout/fee BTC address.
  * NEVER dump the worker fleet as "our miners". Only pool-WIDE aggregates are
    shown on the landing page. Per-address worker detail is only returned for an
    address explicitly queried at /users/<addr>.
  * No RPC creds, no internal IPs/hostnames, no infra/hosting jargon, no
    operator/admin identifiers anywhere in user-visible output.
"""

import json
import math
import re
import html
import time
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Config (public-safe constants only)
# ---------------------------------------------------------------------------
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8201

CKPOOL_STATS_URL = os.getenv("SOLOLUCK_CKPOOL_STATS_URL", "http://127.0.0.1:8888/api/stats")
CKPOOL_LOG = "/var/log/ckpool/ckpool.log"
CKPOOL_BLOCKS_FILE = "/var/log/ckpool/blocks"

# Public-facing connection details (the public stratum endpoint IS public).
POOL_NAME = "SoloLuck"
POOL_TAGLINE = "Asia's community solo Bitcoin pool"
POOL_PITCH = ("Mine to your own address. Strike a block and you keep the whole "
              "reward minus a flat 2% fee — paid straight to you on-chain. "
              "Non-custodial: no account, no KYC, we never hold your coins.")
POOL_FEE_PCT = 2
STRATUM_HOST = os.getenv("SOLOLUCK_STRATUM_HOST", "127.0.0.1")  # your public stratum host/IP
STRATUM_PORT_GENERAL = 3333
STRATUM_PORT_HIGHDIFF = 4334

# Connection tiers (public-safe). The Standard tier is gated by STANDARD_LIVE:
# while False we never advertise a usable stratum URL there (Copy disabled,
# marked 'coming soon') so we never point miners at a port that does not accept
# stratum.
STANDARD_LIVE = True
STRATUM_PORT_STANDARD = 8081

STATS_TIMEOUT = 4  # seconds

# A bare BTC-address shape. We only ever *match* an address the visitor supplied;
# we never enumerate or reveal any address ourselves.
BTC_ADDR_RE = re.compile(r"^(bc1[a-z0-9]{20,90}|[13][a-km-zA-HJ-NP-Z1-9]{20,40})$")


# ---------------------------------------------------------------------------
# Hashrate helpers
# ---------------------------------------------------------------------------
_HASH_UNITS = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}


def parse_hashrate(s):
    """ckpool gives e.g. '22.4T' -> float hashes/s. Returns 0.0 on failure."""
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    m = re.match(r"^([0-9.]+)\s*([KMGTPE]?)", s, re.I)
    if not m:
        return 0.0
    try:
        val = float(m.group(1))
    except ValueError:
        return 0.0
    return val * _HASH_UNITS.get(m.group(2).upper(), 1)


def _words(n):
    n = float(n or 0)
    if n >= 1e12: return "%.1f trillion" % (n / 1e12)
    if n >= 1e9:  return "%.1f billion" % (n / 1e9)
    if n >= 1e6:  return "%.1f million" % (n / 1e6)
    if n >= 1e3:  return "%.0f thousand" % (n / 1e3)
    return "%.0f" % n


def _one_in(fr):
    return ("1 in %s" % _words(1.0 / fr)) if (fr and fr > 0) else "--"


def _years_words(s):
    if not s or s <= 0 or not math.isfinite(s):
        return "--"
    y = s / 31557600.0
    if y < 1:
        d = s / 86400.0
        return ("%d days" % round(d)) if d >= 1 else ("%d hours" % round(s / 3600.0))
    return "≈ %s years" % _words(y)


def _human_pct(p):
    p = float(p or 0)
    if p <= 0: return "0%"
    if p >= 1: return "%.2f%%" % p
    d = min(12, max(2, 2 - int(math.floor(math.log10(p)))))
    return ("%.*f" % (d, p)).rstrip("0").rstrip(".") + "%"


# Network context (difficulty / network hashrate / subsidy) from the local admin
# dashboard, cached — so each public page hit doesn't re-query.
_ADMIN_STATE = {"t": 0.0, "v": None}


def _admin_state():
    """Cached full snapshot from the internal :8200 dashboard. The network
    context AND the rolling pool-hashrate history both live in this payload, so a
    single (cached) fetch feeds both. Returns {} on failure. Nothing from this is
    served raw to the public — callers pick out only sanitized fields."""
    now = time.time()
    if _ADMIN_STATE["v"] is not None and now - _ADMIN_STATE["t"] < 60:
        return _ADMIN_STATE["v"]
    d = {}
    try:
        with urllib.request.urlopen(os.getenv("SOLOLUCK_ADMIN_STATE_URL", "http://127.0.0.1:8200/api/state"), timeout=5) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        d = {}
    _ADMIN_STATE["v"] = d
    _ADMIN_STATE["t"] = now
    return d


_NETCTX = {"t": 0.0, "v": None}


def network_ctx():
    now = time.time()
    if _NETCTX["v"] is not None and now - _NETCTX["t"] < 60:
        return _NETCTX["v"]
    ctx = {}
    try:
        d = _admin_state()
        n = d.get("node", {}) or {}
        L = d.get("lottery", {}) or {}
        ctx = {"difficulty": n.get("difficulty") or L.get("difficulty"),
               "nethash": L.get("network_hashrate") or n.get("networkhashps"),
               "subsidy": n.get("subsidy")}
    except Exception:
        ctx = {}
    _NETCTX["v"] = ctx
    _NETCTX["t"] = now
    return ctx


# ── pool hashrate history (powers the landing-page chart) ─────────────────────
# A self-contained, 10-minute-bucketed ring of pool-WIDE hashrate kept for 24h.
# Seeded once from the internal sampler's fine-grained history (so the chart is
# populated instantly), then maintained by our own 60s sampler thread. The public
# series exposes ONLY aggregate {t, hr, w} — no operator address, no node
# internals, no per-IP data. Buckets store a running MEAN of instantaneous
# hashrate, so the in-progress bucket is valid even when partial (unlike a
# share-accumulation bucket) and the chart can stay live to the latest point.
POOL_HISTORY_PATH = os.getenv("SOLOLUCK_POOL_HISTORY",
                              "/opt/coregrid-pool-public/pool_history.json")
HIST_BUCKET_SEC = 600          # 10-minute buckets (matches public-pool.io)
HIST_MAX_BUCKETS = 144         # 144 x 10min = 24h
_hist_lock = threading.Lock()


def _bucket_key(t):
    return int(t // HIST_BUCKET_SEC) * HIST_BUCKET_SEC


def _load_pool_history():
    try:
        with open(POOL_HISTORY_PATH) as f:
            h = json.load(f)
            return h if isinstance(h, list) else []
    except Exception:
        return []


def _save_pool_history(h):
    tmp = POOL_HISTORY_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(h, f)
        os.replace(tmp, POOL_HISTORY_PATH)
    except Exception:
        pass


def _record_pool_sample(ts, hr, w):
    """Fold one (timestamp, hashrate H/s, worker-count) reading into the ring,
    updating the in-progress 10-min bucket in place (running mean)."""
    try:
        hr = float(hr or 0)
    except (TypeError, ValueError):
        return
    if hr <= 0:
        return
    try:
        w = int(w or 0)
    except (TypeError, ValueError):
        w = 0
    key = _bucket_key(ts)
    with _hist_lock:
        h = _load_pool_history()
        if h and h[-1].get("t") == key:
            b = h[-1]
            n = b.get("n", 1)
            b["hr"] = (b.get("hr", hr) * n + hr) / (n + 1)
            b["n"] = n + 1
            b["w"] = max(b.get("w", 0), w)
        else:
            h.append({"t": key, "hr": hr, "w": w, "n": 1})
            if len(h) > HIST_MAX_BUCKETS:
                h = h[-HIST_MAX_BUCKETS:]
        _save_pool_history(h)


def _backfill_pool_history():
    """One-time seed of the 10-min ring from the internal dashboard's fine (10s)
    rolling history, so the chart shows real data from the first page view."""
    if len(_load_pool_history()) >= 3:
        return
    pts = (_admin_state() or {}).get("history") or []
    buckets = {}
    for p in pts:
        try:
            t = int(p["t"]); hr = float(p["hr"]); w = int(p.get("w") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if hr <= 0:
            continue
        k = _bucket_key(t)
        b = buckets.get(k)
        if b is None:
            buckets[k] = {"t": k, "hr": hr, "w": w, "n": 1}
        else:
            b["hr"] = (b["hr"] * b["n"] + hr) / (b["n"] + 1)
            b["n"] += 1
            b["w"] = max(b["w"], w)
    seeded = [buckets[k] for k in sorted(buckets)][-HIST_MAX_BUCKETS:]
    if seeded:
        with _hist_lock:
            if len(_load_pool_history()) < 3:
                _save_pool_history(seeded)


def pool_history():
    """Public, sanitized hashrate series for the chart: list of {t, hr, w}
    oldest->newest, <=144 points. Keeps the in-progress bucket (our buckets are
    running means, valid even when partial) so the chart stays live."""
    return [{"t": int(b.get("t", 0)),
             "hr": round(float(b.get("hr", 0)), 2),
             "w": int(b.get("w", 0))}
            for b in _load_pool_history() if b.get("hr")]


# --- per-address 24h history (bounded) -------------------------------------
ADDR_HISTORY_PATH = os.getenv("SOLOLUCK_ADDR_HISTORY",
                              "/opt/coregrid-pool-public/addr_history.json")
ADDR_HIST_MAX_ADDRS = 300      # cap distinct addresses tracked (bounds file size)
_addr_lock = threading.Lock()


def _load_addr_history():
    try:
        with open(ADDR_HISTORY_PATH) as f:
            h = json.load(f)
            return h if isinstance(h, dict) else {}
    except Exception:
        return {}


def _save_addr_history(h):
    tmp = ADDR_HISTORY_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(h, f)
        os.replace(tmp, ADDR_HISTORY_PATH)
    except Exception:
        pass


def _record_addr_samples(ts, agg):
    """agg: {address: (hashrate_sum H/s, worker_count)}. Fold each into its own
    10-min running-mean ring (same bucketing as the pool ring). Bounded to
    <=144 buckets/address and <=ADDR_HIST_MAX_ADDRS addresses (evict least-recent)."""
    if not agg:
        return
    key = _bucket_key(ts)
    cutoff = key - HIST_MAX_BUCKETS * HIST_BUCKET_SEC
    with _addr_lock:
        h = _load_addr_history()
        for addr, (hr, w) in agg.items():
            try:
                hr = float(hr or 0)
            except (TypeError, ValueError):
                continue
            if hr <= 0:
                continue
            try:
                w = int(w or 0)
            except (TypeError, ValueError):
                w = 0
            ring = h.get(addr) or []
            if ring and ring[-1].get("t") == key:
                b = ring[-1]; n = b.get("n", 1)
                b["hr"] = (b.get("hr", hr) * n + hr) / (n + 1)
                b["n"] = n + 1
                b["w"] = max(b.get("w", 0), w)
            else:
                ring.append({"t": key, "hr": hr, "w": w, "n": 1})
            h[addr] = [b for b in ring if b.get("t", 0) > cutoff][-HIST_MAX_BUCKETS:]
        h = {a: r for a, r in h.items() if r}                  # drop emptied rings
        if len(h) > ADDR_HIST_MAX_ADDRS:                       # evict least-recently-active
            ranked = sorted(h.items(), key=lambda kv: kv[1][-1].get("t", 0), reverse=True)
            h = dict(ranked[:ADDR_HIST_MAX_ADDRS])
        _save_addr_history(h)


def addr_history(address):
    """Sanitized per-address series {t,hr,w} oldest->newest for the SSR sparkline."""
    ring = _load_addr_history().get(address) or []
    return [{"t": int(b.get("t", 0)), "hr": round(float(b.get("hr", 0)), 2),
             "w": int(b.get("w", 0))} for b in ring if b.get("hr")]


def _pool_history_sampler():
    """Background thread: backfill once, then sample pool hashrate every 60s."""
    try:
        _backfill_pool_history()
    except Exception:
        pass
    while True:
        try:
            stats = fetch_stats()
            sd = stats if isinstance(stats, dict) else {}
            pool = sd.get("pool", {})
            now = time.time()
            _record_pool_sample(now,
                                parse_hashrate(pool.get("hashrate1m")),
                                pool.get("Workers"))
            # aggregate the same worker list per base-address (no extra fetch)
            agg = {}
            for wk in (sd.get("workers") or []):
                name = str(wk.get("workername") or wk.get("name") or "")
                base = name.split(".", 1)[0]
                if not base:
                    continue
                hr, wc = agg.get(base, (0.0, 0))
                agg[base] = (hr + parse_hashrate(wk.get("hashrate1m")), wc + 1)
            _record_addr_samples(now, agg)
        except Exception:
            pass
        time.sleep(60)


def fmt_hashrate(h):
    """float hashes/s -> human string like '22.4 TH/s'."""
    h = float(h or 0)
    for unit, scale in (("EH/s", 1e18), ("PH/s", 1e15), ("TH/s", 1e12),
                        ("GH/s", 1e9), ("MH/s", 1e6), ("KH/s", 1e3)):
        if h >= scale:
            return "%.2f %s" % (h / scale, unit)
    return "%.0f H/s" % h


# ---------------------------------------------------------------------------
# Data acquisition (read-only)
# ---------------------------------------------------------------------------
def fetch_stats():
    """Fetch ckpool stats JSON. Returns dict or None."""
    try:
        req = urllib.request.Request(CKPOOL_STATS_URL,
                                     headers={"User-Agent": "sololuck-public"})
        with urllib.request.urlopen(req, timeout=STATS_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


# Cache for read_solved_blocks(): scanning the multi-MB ckpool.log on every
# request was the entire page latency (~1.6s) and caused pile-ups/timeouts under
# concurrent load. Solved blocks change essentially never, so a short TTL is safe.
_BLOCKS_CACHE = {"t": 0.0, "v": None}
_BLOCKS_TTL = 120  # seconds


def read_solved_blocks():
    """
    Return a list of pool-SOLVED blocks (public, celebratory info only):
        [{"height": int|None, "hash": str|None, "when": str|None}, ...]
    We scan ckpool's own solved-block markers. We deliberately IGNORE
    'ZMQ block hash' / 'Block hash changed' lines -- those are network block
    notifications, NOT blocks this pool found.

    Result is cached for _BLOCKS_TTL seconds to avoid re-scanning the (large,
    ever-growing) ckpool log on every page hit.
    """
    now = time.time()
    if _BLOCKS_CACHE["v"] is not None and (now - _BLOCKS_CACHE["t"]) < _BLOCKS_TTL:
        return _BLOCKS_CACHE["v"]

    blocks = []
    seen = set()

    # 1) Optional dedicated blocks file (ckpool writes one when configured).
    try:
        with open(CKPOOL_BLOCKS_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = {"height": None, "hash": None, "when": None}
                try:
                    obj = json.loads(line)
                    rec["height"] = obj.get("height") or obj.get("blockheight")
                    rec["hash"] = obj.get("hash") or obj.get("blockhash")
                    rec["when"] = obj.get("when") or obj.get("createdate")
                except Exception:
                    hh = re.search(r"\b(\d{6,8})\b", line)
                    if hh:
                        rec["height"] = int(hh.group(1))
                    hx = re.search(r"\b([0-9a-f]{64})\b", line)
                    if hx:
                        rec["hash"] = hx.group(1)
                key = rec["hash"] or rec["height"]
                if key and key not in seen:
                    seen.add(key)
                    blocks.append(rec)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # 2) Canonical solved-block lines in the main ckpool log.
    #    ckpool emits things like "Solved and confirmed block 840000 by ..." /
    #    "BLOCK ACCEPTED!" when this pool solves a block.
    solve_re = re.compile(
        r"(BLOCK ACCEPTED|Solved and confirmed block|Block solve|Solved block)",
        re.I)
    try:
        with open(CKPOOL_LOG, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not solve_re.search(line):
                    continue
                ts = re.match(r"\[([^\]]+)\]", line)
                hh = re.search(r"block\s+(\d{6,8})", line, re.I)
                hx = re.search(r"\b([0-9a-f]{64})\b", line)
                rec = {
                    "height": int(hh.group(1)) if hh else None,
                    "hash": hx.group(1) if hx else None,
                    "when": ts.group(1) if ts else None,
                }
                key = rec["hash"] or rec["height"] or line.strip()
                if key not in seen:
                    seen.add(key)
                    blocks.append(rec)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    _BLOCKS_CACHE["v"] = blocks
    _BLOCKS_CACHE["t"] = now
    return blocks


# ---------------------------------------------------------------------------
# Public-safe view models
# ---------------------------------------------------------------------------
def build_public_view():
    """
    Assemble ONLY public-safe, pool-WIDE data. No per-worker fleet detail,
    no operator address, no node internals.
    """
    stats = fetch_stats()
    pool = (stats or {}).get("pool", {}) if isinstance(stats, dict) else {}

    def gp(k, default=None):
        return pool.get(k, default)

    view = {
        "pool_name": POOL_NAME,
        "tagline": POOL_TAGLINE,
        "fee_pct": POOL_FEE_PCT,
        "online": stats is not None,
        "hashrate": {
            "1m": gp("hashrate1m"),
            "5m": gp("hashrate5m"),
            "1h": gp("hashrate1hr"),
            "1d": gp("hashrate1d"),
        },
        "workers": gp("Workers"),
        # bestshare is a difficulty number, not identifying anyone -> safe.
        "bestshare": gp("bestshare"),
        # network share difficulty target context, if ckpool exposes it.
        "network_diff": None,  # was ckpool share-progress (0.01), misleading as network diff -> hidden
        "network": (lambda nc: {"difficulty": nc.get("difficulty"),
                                "hashrate": nc.get("nethash"),
                                "subsidy": nc.get("subsidy")})(network_ctx()),
        "blocks": read_solved_blocks(),
        "stratum": {
            "host": STRATUM_HOST,
            "port_general": STRATUM_PORT_GENERAL,
        },
        "generated_at": (stats or {}).get("generated_at") if isinstance(stats, dict) else None,
        # rolling 24h pool-wide hashrate series for the landing-page chart
        "history": pool_history(),
    }
    return view


def build_address_view(address):
    """
    Per-address detail. ONLY returned for the address the visitor explicitly
    requested. In true-solo mode usernames are 'address' or 'address.worker'.
    Returns dict {found: bool, address, workers: [...], totals: {...}}.
    """
    out = {"found": False, "address": address, "workers": [], "totals": {}}
    stats = fetch_stats()
    if not isinstance(stats, dict):
        return out
    workers = stats.get("workers") or []

    addr_lower = address.lower()
    matched = []
    for w in workers:
        name = str(w.get("workername") or w.get("name") or "")
        # Match the worker's username against the queried address. A worker is
        # this address's iff name == address OR name == address.worker.
        base = name.split(".", 1)[0]
        if base.lower() == addr_lower:
            matched.append(w)

    if not matched:
        return out

    out["found"] = True
    total_1m = total_5m = total_1h = total_1d = 0.0
    total_shares = 0
    best_ever = 0.0
    last_share = 0
    for w in matched:
        name = str(w.get("workername") or w.get("name") or "")
        # Label only the worker suffix the visitor themselves chose; if bare,
        # show '(default)'. We never decorate with anything internal.
        if "." in name:
            label = name.split(".", 1)[1]
        else:
            label = "(default)"
        h1m = parse_hashrate(w.get("hashrate1m"))
        h5m = parse_hashrate(w.get("hashrate5m"))
        h1h = parse_hashrate(w.get("hashrate1hr"))
        h1d = parse_hashrate(w.get("hashrate1d"))
        shares = int(w.get("shares") or 0)
        bev = float(w.get("bestever") or w.get("bestshare") or 0)
        ls = int(w.get("lastshare") or 0)
        online = bool(w.get("online", h1m > 0))
        total_1m += h1m
        total_5m += h5m
        total_1h += h1h
        total_1d += h1d
        total_shares += shares
        best_ever = max(best_ever, bev)
        last_share = max(last_share, ls)
        out["workers"].append({
            "worker": label,
            "hashrate1m": fmt_hashrate(h1m),
            "hashrate5m": fmt_hashrate(h5m),
            "hashrate1h": fmt_hashrate(h1h),
            "hashrate1d": fmt_hashrate(h1d),
            "shares": shares,
            "bestever": int(bev),
            "lastshare": ls,
            "online": online,
        })
    out["totals"] = {
        "hashrate1m": fmt_hashrate(total_1m),
        "hashrate5m": fmt_hashrate(total_5m),
        "hashrate1h": fmt_hashrate(total_1h),
        "hashrate1d": fmt_hashrate(total_1d),
        "shares": total_shares,
        "bestever": int(best_ever),
        "lastshare": last_share,
        "worker_count": len(matched),
    }

    # Per-miner solo odds — computed for THIS address's hashrate alone (true solo).
    nc = network_ctx()
    diff = nc.get("difficulty"); nethash = nc.get("nethash"); subsidy = nc.get("subsidy")
    uhr = total_1h or total_5m or total_1m   # steadiest available hashrate
    odds = {"eta": "--", "closest": "--", "share": "--", "yield": "--",
            "luck": "--", "nethash": fmt_hashrate(nethash) if nethash else "--"}
    if diff and uhr > 0:
        eta = diff * (2 ** 32) / uhr
        odds["eta"] = _years_words(eta)
        if subsidy:
            sats = subsidy * 86400.0 / eta * 1e8
            odds["yield"] = ("{:,} sats/day".format(int(round(sats))) if sats >= 1
                             else "%d sats/mo" % int(round(sats * 30)))
    if nethash and uhr > 0:
        odds["share"] = _one_in(uhr / nethash)
    if diff and best_ever > 0:
        odds["closest"] = _one_in(best_ever / diff)
    if diff and total_shares > 0:
        odds["luck"] = _human_pct(total_shares / diff * 100.0)
    out["odds"] = odds
    return out


# ---------------------------------------------------------------------------
# HTML / CSS / JS  (rendering layer — minimal-credible, Direction #1)
# ---------------------------------------------------------------------------
PAGE_CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#0b0e14;color:#dfe6f0;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
line-height:1.55;overflow-wrap:anywhere}
a{color:#f7931a;text-decoration:none}a:hover{text-decoration:underline}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.wrap{max-width:860px;margin:0 auto;padding:30px 20px 60px}
header{text-align:center;padding:18px 0 4px}
h1{font-size:30px;margin:0 0 8px;letter-spacing:.4px;font-weight:700}
h1 .b{color:#f7931a}
h1.brand{font-size:40px;margin:0 0 2px;letter-spacing:-.5px}
.tagline{color:#f7931a;font-size:15px;font-weight:600;letter-spacing:.3px;margin:0 0 10px}
.pitch{color:#9fb0c5;max-width:640px;margin:0 auto;font-size:15px}
/* live status strip */
.statusbar{margin:16px auto 4px;font-size:14px;color:#9fb0c5;max-width:680px}
.statusbar .hl{color:#dfe6f0;font-weight:600}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#3ad17a;
margin-right:6px;vertical-align:middle;animation:pulse 1.6s infinite}
.dot.off{background:#d1593a;animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.trust{margin:10px auto 0;max-width:640px;color:#8295ad;font-size:13px}
/* cards */
.card{background:#131826;border:1px solid #1c2436;border-radius:12px;
padding:18px 20px;margin:18px 0}
.card h2{margin:0 0 14px;font-size:17px;color:#f7931a;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:#0e1320;border:1px solid #1c2436;border-radius:10px;padding:12px 14px}
.stat .k{font-size:11px;color:#8295ad;text-transform:uppercase;letter-spacing:.7px}
.stat .v{font-size:21px;font-weight:600;margin-top:4px}
.stat .sub{font-size:11px;color:#8295ad;margin-top:3px;line-height:1.3}
.muted{color:#8295ad;font-size:13px}
.note{color:#9fb0c5;font-size:14px;margin:0 0 14px}
/* tiers */
.tiers{display:grid;grid-template-columns:1fr;gap:14px}
@media(min-width:720px){.tiers{grid-template-columns:repeat(3,1fr)}}
.tier{position:relative;background:#0e1320;border:1px solid #1c2436;border-radius:10px;
padding:16px 16px 14px;border-left:3px solid #2a3550}
.tier.lite{border-left-color:#6f86b8}
.tier.pro{border-left-color:#f7931a}
.tier.std{border:1px solid #f7931a;border-left:3px solid #f7931a;
background:linear-gradient(180deg,rgba(247,147,26,.07),rgba(247,147,26,.02))}
.tier .tname{font-weight:700;font-size:15px;color:#dfe6f0;margin:0 0 2px}
.tier .role{color:#9fb0c5;font-size:13px;margin:0 0 10px}
.tier .urlrow{display:flex;gap:6px;align-items:stretch;margin:0 0 10px}
.tier .url{flex:1;min-width:0;background:#0b0e14;border:1px solid #1c2436;border-radius:7px;
padding:8px 9px;font-size:12px;color:#7fd1a8;word-break:break-all}
.tier .url.dim{color:#5d6b80}
.copy{flex:none;background:#1c2436;color:#dfe6f0;border:1px solid #2a3550;border-radius:7px;
padding:0 11px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}
.copy:hover{background:#222c3f}
.copy[disabled]{opacity:.4;cursor:not-allowed}
.tier .meta{font-size:12.5px;color:#c2cee0;margin:2px 0}
.tier .meta b{color:#dfe6f0}
.tier .egs{font-size:12px;color:#8295ad;margin:8px 0 0}
.ribbon{position:absolute;top:-9px;right:12px;background:#f7931a;color:#0b0e14;
font-size:10px;font-weight:700;letter-spacing:.6px;border-radius:20px;padding:3px 9px}
.soon{position:absolute;top:-9px;left:12px;background:#2a3550;color:#9fb0c5;
font-size:10px;font-weight:700;letter-spacing:.6px;border-radius:20px;padding:3px 9px}
.login{margin:16px 0 0;padding:13px 15px;background:#0b0e14;border:1px solid #1c2436;
border-radius:9px;font-size:13.5px;color:#c2cee0}
.login code{color:#7fd1a8}
/* lookup */
.lookform{display:flex;flex-wrap:wrap;gap:9px;margin-top:6px}
input{background:#0e1320;border:1px solid #2a3550;color:#dfe6f0;border-radius:8px;
padding:10px 12px;font-family:ui-monospace,monospace;font-size:13px;flex:1;min-width:200px}
input:focus{border-color:#f7931a}
a:focus-visible,button:focus-visible,summary:focus-visible,input:focus-visible,.langtoggle a:focus-visible{outline:2px solid #f7931a;outline-offset:2px;border-radius:4px}
.btn{background:#f7931a;color:#0b0e14;border:none;border-radius:8px;padding:10px 18px;
font-weight:700;cursor:pointer;font-size:14px}
.btn:hover{opacity:.92}
.cta{display:inline-block;text-decoration:none;font-size:15px;padding:11px 22px}
.cta:hover{text-decoration:none}
/* fee */
.feebig{font-size:46px;font-weight:700;color:#f7931a;line-height:1;margin:2px 0 4px}
/* blocks + tables */
.blocks{margin:6px 0;padding-left:20px}
.blocks li{font-family:ui-monospace,monospace;font-size:13px;margin:4px 0;color:#c2cee0}
ul.bullets{margin:6px 0;padding-left:20px}
ul.bullets li{margin:5px 0;color:#c2cee0}
table{width:100%;border-collapse:collapse;font-size:13px}
.tblwrap{overflow-x:auto}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #1c2436;white-space:nowrap}
th{color:#8295ad;font-weight:500;font-size:11px;text-transform:uppercase}
footer{text-align:center;color:#5d6b80;font-size:12px;margin-top:32px}
details{border-bottom:1px solid #1c2436;padding:10px 0}
details:last-child{border-bottom:0}
summary{cursor:pointer;font-weight:600;color:#dfe6f0;list-style:none}
summary::-webkit-details-marker{display:none}
summary:before{content:"+ ";color:#f7931a;font-weight:700}
details[open] summary:before{content:"– "}
details p{margin:8px 0 2px}
#calcUnit{background:#0b0e14;color:#dfe6f0;border:1px solid #1c2436;border-radius:8px}
.langtoggle{display:flex;gap:6px;justify-content:flex-end;margin:0 0 8px}
.langtoggle{flex-wrap:wrap}
.langtoggle a{background:#131a26;border:1px solid #1c2436;border-radius:7px;padding:3px 8px;cursor:pointer;font-size:17px;line-height:1.1;text-decoration:none;filter:grayscale(.5);opacity:.7;transition:all .15s}
.langtoggle a:hover{filter:none;opacity:1}
.langtoggle a.on{background:#f7931a;border-color:#f7931a;filter:none;opacity:1;box-shadow:0 0 0 2px rgba(247,147,26,.3)}
/* pool hashrate chart */
.hrwrap{position:relative;margin-top:4px}
#hrSvg{width:100%;height:auto;display:block;touch-action:none;cursor:crosshair}
.hrgrid line{stroke:#1b2233;stroke-width:1}
.hraxis text{fill:#7a8699;font-size:11px}
.hr-empty{position:absolute;top:50%;left:0;right:0;text-align:center;transform:translateY(-50%);color:#5d6b80;font-size:13px;pointer-events:none}
.hr-tip{position:absolute;pointer-events:none;z-index:5;background:#0e1320;border:1px solid #243049;border-radius:8px;padding:7px 10px;font-size:12px;color:#dfe6f0;min-width:128px;box-shadow:0 6px 22px rgba(0,0,0,.45)}
.hr-tip .t{color:#8295ad}
.hr-tip .hv{font-size:15px;font-weight:700;color:#f7931a;margin:1px 0}
.hr-legend{display:flex;flex-wrap:wrap;gap:16px;align-items:center;justify-content:center;margin-top:10px;color:#9fb0c5;font-size:12px}
.hr-legend i.sw{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:middle}
"""


def fmt_unix(ts):
    if not ts:
        return "—"
    try:
        import datetime
        return datetime.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


def fmt_int(n):
    """Thousands-separated integer, or em-dash."""
    if n is None or n == "":
        return "—"
    try:
        return "{:,}".format(int(float(n)))
    except (ValueError, TypeError):
        return html.escape(str(n))


def _connect_card():
    """
    Three-tier connect section. Standard (:8081) is gated by STANDARD_LIVE:
    while False we never advertise a usable stratum URL there (Copy disabled,
    marked 'coming soon') because the port does not yet accept stratum.
    """
    lite_url = "stratum+tcp://%s:%d" % (STRATUM_HOST, STRATUM_PORT_GENERAL)
    pro_url = "stratum+tcp://%s:%d" % (STRATUM_HOST, STRATUM_PORT_HIGHDIFF)
    std_url = "stratum+tcp://%s:%d" % (STRATUM_HOST, STRATUM_PORT_STANDARD)

    # Standard tier varies by whether the port is genuinely live.
    if STANDARD_LIVE:
        std_badge = "<span class='ribbon'>START HERE</span>"
        std_url_cls = "url"
        std_copy = ("<button class='copy' type='button' "
                    "data-copy='%s'>Copy</button>" % html.escape(std_url))
        std_url_txt = html.escape(std_url)
    else:
        std_badge = ("<span class='ribbon'>START HERE</span>"
                     "<span class='soon'>COMING SOON</span>")
        std_url_cls = "url dim"
        std_copy = "<button class='copy' type='button' disabled>Soon</button>"
        std_url_txt = html.escape(std_url) + " — not live yet"

    return """
<div class="card" id="connect">
  <h2>Connect — pick the port that matches your gear.</h2>
  <p class="note">The port only sets your <b>starting</b> difficulty. Vardiff tunes it automatically afterward, so just pick the closest match.</p>
  <div class="tiers">

    <div class="tier lite">
      <p class="tname">Lite</p>
      <p class="role">Tiny &amp; hobby rigs, &lt; ~2 TH/s</p>
      <div class="urlrow">
        <div class="url mono">%(lite_url)s</div>
        <button class="copy" type="button" data-copy="%(lite_url)s">Copy</button>
      </div>
      <p class="meta">Start difficulty <b>1,024</b> · vardiff</p>
      <p class="egs">USB sticks · NerdMiner · Bitaxe · single Avalon Nano</p>
    </div>

    <div class="tier std">
      %(std_badge)s
      <p class="tname">Standard</p>
      <p class="role">Home &amp; small-farm ASICs, ~2–200 TH/s</p>
      <div class="urlrow">
        <div class="%(std_url_cls)s mono">%(std_url_txt)s</div>
        %(std_copy)s
      </div>
      <p class="meta">Start difficulty <b>131,072</b> · vardiff</p>
      <p class="egs">Antminer S9/S19 · Whatsminer · Avalon 12xx</p>
    </div>

    <div class="tier pro">
      <p class="tname">Pro</p>
      <p class="role">Modern &amp; clustered ASICs, 200 TH/s+</p>
      <div class="urlrow">
        <div class="url mono">%(pro_url)s</div>
        <button class="copy" type="button" data-copy="%(pro_url)s">Copy</button>
      </div>
      <p class="meta">Start difficulty <b>1,048,576</b> · vardiff</p>
      <p class="egs">S21/S21 XP · M60 series · multi-rig farms</p>
    </div>

  </div>
  <div class="login">
    <b>How to log in.</b> Username = your own BTC address (optionally
    <code>address.workername</code> per rig). Password = anything, e.g.
    <code>x</code>. The port only sets your STARTING difficulty — vardiff tunes
    it automatically after that, so just pick the closest match. Invalid
    addresses are rejected. Solve a block and the reward is paid straight to
    your address on-chain.
  </div>
  <div class="login" style="margin-top:10px">
    <b>On a Bitaxe (AxeOS), NerdQAxe or Avalon?</b> Those take the host and port
    in <i>separate</i> fields — with no <code>stratum+tcp://</code> prefix. Use
    Host <code>%(host)s</code>
    <button class="copy" type="button" data-copy="%(host)s">Copy</button>
    and Port <code>3333</code> Lite / <code>8081</code> Standard /
    <code>4334</code> Pro.
  </div>
</div>""" % {
        "host": html.escape(STRATUM_HOST),
        "lite_url": html.escape(lite_url),
        "pro_url": html.escape(pro_url),
        "std_badge": std_badge,
        "std_url_cls": std_url_cls,
        "std_url_txt": std_url_txt,
        "std_copy": std_copy,
    }


# Multi-language support for the public landing page. English render is produced
# normally; other languages are made by post-process phrase replacement (keys match
# the RENDERED HTML, incl. entities like &amp; / &#x27;). Crypto/technical terms
# (hashrate, stratum, BTC, ASIC, KYC, vardiff, TH/s, SoloLuck) stay in English by
# design — that's how Asian mining communities read them, and it keeps it natural.
# Translations are a solid first pass; a native speaker can refine any phrase by
# editing the tuple here.
SUPPORTED_LANGS = ["en", "id", "ms", "ja", "th", "ko", "zh", "vi", "tl", "hi"]
LANG_BTNS = [("en", "🇬🇧", "English"), ("id", "🇮🇩", "Bahasa Indonesia"),
             ("ms", "🇲🇾", "Bahasa Melayu"), ("ja", "🇯🇵", "日本語"), ("th", "🇹🇭", "ไทย"),
             ("ko", "🇰🇷", "한국어"), ("zh", "🇨🇳", "中文"), ("vi", "🇻🇳", "Tiếng Việt"),
             ("tl", "🇵🇭", "Filipino"), ("hi", "🇮🇳", "हिन्दी")]
_LO = ["id", "ja", "th", "ko", "zh", "vi"]   # translation column order

# hreflang code per lang (BCP-47) + og:locale + a per-language meta/og/twitter
# description (one string used for all three tags so SERP snippets and social
# cards localize together). Translations are a solid first pass.
HREFLANG = {"en": "en", "id": "id", "ms": "ms", "ja": "ja", "th": "th",
            "ko": "ko", "zh": "zh-Hans", "vi": "vi", "tl": "fil", "hi": "hi"}
OG_LOCALE = {"en": "en_US", "id": "id_ID", "ms": "ms_MY", "ja": "ja_JP",
             "th": "th_TH", "ko": "ko_KR", "zh": "zh_CN", "vi": "vi_VN",
             "tl": "fil_PH", "hi": "hi_IN"}
DESCRIPTIONS = {
    "en": "SoloLuck — Asia's community solo Bitcoin pool. Mine to your own address; strike a block and keep the whole reward minus a flat 2% fee. Non-custodial: no account, no KYC.",
    "id": "SoloLuck — pool solo Bitcoin komunitas Asia. Menambang ke alamat Anda sendiri; temukan blok dan simpan seluruh hadiahnya dikurangi biaya tetap 2%. Non-kustodian: tanpa akun, tanpa KYC.",
    "ms": "SoloLuck — pool solo Bitcoin komuniti Asia. Lombong ke alamat anda sendiri; jumpa blok dan simpan seluruh ganjaran tolak yuran tetap 2%. Bukan kustodian: tiada akaun, tiada KYC.",
    "ja": "SoloLuck — アジアのコミュニティ・ソロ Bitcoin プール。自分のアドレスにマイニングし、ブロックを掘り当てれば一律2%の手数料を除いた報酬すべてが自分のものに。ノンカストディアル、アカウント不要、KYC 不要。",
    "th": "SoloLuck — พูลขุด Bitcoin แบบโซโลของชุมชนเอเชีย ขุดเข้าที่อยู่ของคุณเอง เจอบล็อกแล้วได้รางวัลทั้งหมดหักค่าธรรมเนียมคงที่ 2% ไม่ดูแลเหรียญแทน ไม่ต้องสมัคร ไม่ต้อง KYC",
    "ko": "SoloLuck — 아시아 커뮤니티 솔로 비트코인 풀. 본인 주소로 채굴하고, 블록을 찾으면 고정 2% 수수료를 뺀 전체 보상이 내 것. 비수탁형, 계정 불필요, KYC 불필요.",
    "zh": "SoloLuck — 亚洲社区单人 Bitcoin 矿池。挖矿至你自己的地址；挖到区块即可保留全部奖励，仅扣除固定 2% 费用。非托管：无需账户，无需 KYC。",
    "vi": "SoloLuck — pool đào Bitcoin solo của cộng đồng châu Á. Đào về địa chỉ của chính bạn; tìm được khối là giữ trọn phần thưởng trừ phí cố định 2%. Phi lưu ký: không tài khoản, không KYC.",
    "tl": "SoloLuck — community solo Bitcoin pool ng Asia. Mag-mine sa sarili mong address; makahanap ng block at panatilihin ang buong reward bawas ang flat na 2% fee. Non-custodial: walang account, walang KYC.",
    "hi": "SoloLuck — एशिया का कम्युनिटी सोलो Bitcoin पूल। अपने ही address पर माइन करें; ब्लॉक मिलने पर सिर्फ़ 2% फ़ीस घटाकर पूरा इनाम आपका। नॉन-कस्टोडियल: कोई अकाउंट नहीं, कोई KYC नहीं।",
}


def _head_i18n(lang):
    """Per-language SEO head fragments: self-referential canonical, full
    hreflang cluster (+ x-default), og:locale (+ alternates), description."""
    def url(L):
        return "https://sololuck.io/" if L == "en" else ("https://sololuck.io/?lang=%s" % L)
    hreflang = "".join('<link rel="alternate" hreflang="%s" href="%s">'
                       % (HREFLANG.get(L, L), url(L)) for L in SUPPORTED_LANGS)
    hreflang += '<link rel="alternate" hreflang="x-default" href="https://sololuck.io/">'
    alt = "".join('<meta property="og:locale:alternate" content="%s">' % OG_LOCALE[L]
                  for L in SUPPORTED_LANGS if L != lang)
    return {"canon": url(lang), "hreflang": hreflang,
            "desc": html.escape(DESCRIPTIONS.get(lang, DESCRIPTIONS["en"]), quote=True),
            "oglocale": OG_LOCALE.get(lang, "en_US"), "oglocale_alt": alt}


_FAQ_LD = [
    ("What is solo mining?",
     "You mine for whole blocks on your own. No small steady payouts, but if your miner solves a block the entire reward (about 3.125 BTC plus fees) is yours, paid straight to your address. A lottery with a very big prize."),
    ("Why SoloLuck instead of going solo at home?",
     "We keep a fast, well-connected node close to Asia, so a block you find reaches the network instantly (less orphan risk). You skip running and syncing your own node, just point your miner at us."),
    ("How does the 2% fee work?",
     "Only if you solve a block. The 2% is taken once, inside that block's own coinbase, on-chain and in the open. No block, no fee, and we never hold your coins."),
    ("What username and password do I use?",
     "Your own BTC address (bech32 bc1q...) as the username. Add .workername to track multiple rigs. The password can be anything."),
    ("What hardware works?",
     "Any SHA-256 ASIC: Bitaxe, NerdQAxe, Avalon, Antminer and the like. Pick the port that matches your hashrate; vardiff tunes the rest. About 100 GH/s is a sensible minimum."),
    ("Is it safe and non-custodial?",
     "Yes. We never hold your coins, no balance, no withdrawal. A found block pays directly to the address you mine with. No account, no KYC, no trackers."),
    ("When and how do I get paid?",
     "The instant you solve a block, the network pays its coinbase straight to your address. That is the only payout, solo is all-or-nothing."),
]


def _json_ld():
    """Organization + WebSite + FAQPage JSON-LD for rich snippets. Built from
    the same FAQ copy the page renders so they never drift. English-only (kept
    out of the post-process translation path to avoid corrupting the JSON)."""
    blocks = [
        {"@context": "https://schema.org", "@type": "Organization", "name": "SoloLuck",
         "url": "https://sololuck.io/", "logo": "https://sololuck.io/favicon.svg",
         "description": DESCRIPTIONS["en"]},
        {"@context": "https://schema.org", "@type": "WebSite", "name": "SoloLuck",
         "url": "https://sololuck.io/"},
        {"@context": "https://schema.org", "@type": "FAQPage",
         "mainEntity": [{"@type": "Question", "name": q,
                         "acceptedAnswer": {"@type": "Answer", "text": a}}
                        for q, a in _FAQ_LD]},
    ]
    return "".join('<script type="application/ld+json">%s</script>'
                   % json.dumps(b, ensure_ascii=False) for b in blocks)

# (english_in_rendered_html, (id, ja, th, ko, zh, vi))
_TR_RAW = [
    # --- hero ---
    ("Asia&#x27;s community solo Bitcoin pool",
     ("Pool solo Bitcoin komunitas Asia", "アジアのコミュニティ・ソロ Bitcoin プール",
      "พูลโซโล Bitcoin ของชุมชนเอเชีย", "아시아 커뮤니티 솔로 비트코인 풀",
      "亚洲社区单独挖矿比特币矿池", "Pool solo Bitcoin cộng đồng châu Á")),
    ("Asia's community solo Bitcoin pool",
     ("Pool solo Bitcoin komunitas Asia", "アジアのコミュニティ・ソロ Bitcoin プール",
      "พูลโซโล Bitcoin ของชุมชนเอเชีย", "아시아 커뮤니티 솔로 비트코인 풀",
      "亚洲社区单独挖矿比特币矿池", "Pool solo Bitcoin cộng đồng châu Á")),
    (POOL_PITCH,
     ("Tambang ke alamat Anda sendiri. Temukan satu blok dan seluruh hadiahnya jadi milik Anda — dikurangi biaya flat 2%, dibayar langsung ke Anda secara on-chain. Non-kustodial: tanpa akun, tanpa KYC, kami tidak pernah memegang koin Anda.",
      "自分のアドレスで採掘。ブロックを見つければ報酬は丸ごとあなたのもの — 一律2%の手数料を引いた額がオンチェーンで直接支払われます。ノンカストディアル：アカウント不要・KYC不要・あなたのコインを預かりません。",
      "ขุดไปยังที่อยู่ของคุณเอง เจอบล็อกแล้วรางวัลทั้งหมดเป็นของคุณ — หักค่าธรรมเนียมคงที่ 2% จ่ายตรงถึงคุณบนเชน ไม่เก็บรักษาเหรียญ ไม่ต้องมีบัญชี ไม่ต้อง KYC เราไม่เคยถือเหรียญของคุณ",
      "자신의 주소로 채굴하세요. 블록을 찾으면 보상 전체가 당신의 것입니다 — 일률 2% 수수료만 제외하고 온체인으로 바로 지급됩니다. 비수탁: 계정·KYC 없음, 당신의 코인을 보관하지 않습니다.",
      "用你自己的地址挖矿。挖到区块，全部奖励归你 — 仅扣固定 2% 费用，直接在链上支付给你。非托管：无需账户、无需 KYC，我们从不保管你的币。",
      "Đào về địa chỉ của chính bạn. Tìm được một khối thì toàn bộ phần thưởng là của bạn — trừ phí cố định 2%, trả thẳng cho bạn on-chain. Không giữ hộ: không tài khoản, không KYC, không bao giờ giữ coin của bạn.")),
    ("No account. No KYC. No custodian — your address is your payout. Honest stats, real odds, real blocks.",
     ("Tanpa akun. Tanpa KYC. Tanpa kustodian — alamat Anda adalah pembayaran Anda. Statistik jujur, peluang nyata, blok nyata.",
      "アカウント不要。KYC不要。カストディアンなし — あなたのアドレスが支払先です。正直な統計、本物の確率、本物のブロック。",
      "ไม่ต้องมีบัญชี ไม่ต้อง KYC ไม่มีผู้ดูแลเหรียญ — ที่อยู่ของคุณคือที่รับเงิน สถิติจริง โอกาสจริง บล็อกจริง",
      "계정 없음. KYC 없음. 수탁자 없음 — 당신의 주소가 곧 지급처입니다. 정직한 통계, 진짜 확률, 진짜 블록.",
      "无需账户。无需 KYC。无托管方 — 你的地址就是收款地址。真实数据、真实概率、真实区块。",
      "Không tài khoản. Không KYC. Không bên giữ hộ — địa chỉ của bạn là nơi nhận. Số liệu thật, xác suất thật, khối thật.")),
    # --- headings ---
    ("Live pool stats", ("Statistik pool langsung", "ライブ統計", "สถิติพูลแบบสด", "실시간 풀 통계", "实时矿池统计", "Thống kê trực tiếp")),
    ("Solo odds calculator", ("Kalkulator peluang solo", "ソロ確率計算ツール", "เครื่องคำนวณโอกาสโซโล", "솔로 확률 계산기", "单独挖矿概率计算器", "Máy tính xác suất solo")),
    ("Why SoloLuck", ("Kenapa SoloLuck", "SoloLuck を選ぶ理由", "ทำไมต้อง SoloLuck", "SoloLuck를 선택하는 이유", "为什么选择 SoloLuck", "Vì sao chọn SoloLuck")),
    ("Track an address", ("Lacak alamat", "アドレスを追跡", "ติดตามที่อยู่", "주소 추적", "追踪地址", "Theo dõi địa chỉ")),
    ("The fee — flat 2%, nothing hidden", ("Biaya — flat 2%, tanpa yang tersembunyi", "手数料 — 一律2%、隠れた費用なし", "ค่าธรรมเนียม — คงที่ 2% ไม่มีค่าซ่อนเร้น", "수수료 — 일률 2%, 숨김 없음", "费用 — 固定 2%，无隐藏费用", "Phí — cố định 2%, không ẩn phí")),
    ("Found Blocks", ("Blok yang Ditemukan", "発見したブロック", "บล็อกที่พบ", "발견한 블록", "已找到的区块", "Khối đã tìm thấy")),
    ("Rules &amp; guidance", ("Aturan &amp; panduan", "ルールと案内", "กฎและคำแนะนำ", "규칙 및 안내", "规则与指南", "Quy tắc &amp; hướng dẫn")),
    ("Connect — pick the port that matches your gear.",
     ("Hubungkan — pilih port sesuai perangkat Anda.", "接続 — 機材に合うポートを選んでください。",
      "เชื่อมต่อ — เลือกพอร์ตที่ตรงกับอุปกรณ์ของคุณ", "연결 — 장비에 맞는 포트를 선택하세요.",
      "连接 — 选择匹配你设备的端口。", "Kết nối — chọn cổng phù hợp thiết bị của bạn.")),
    # --- stat labels ---
    ("Miners online", ("Penambang online", "オンラインのマイナー", "นักขุดออนไลน์", "온라인 채굴자", "在线矿工", "Thợ đào trực tuyến")),
    ("Best share ever", ("Share terbaik", "最高シェア", "แชร์ที่ดีที่สุด", "최고 셰어", "历史最佳 share", "Share tốt nhất")),
    ("Network hashrate", ("Hashrate jaringan", "ネットワーク hashrate", "Hashrate เครือข่าย", "네트워크 hashrate", "全网 hashrate", "Hashrate mạng lưới")),
    ("Blocks found", ("Blok ditemukan", "発見ブロック数", "บล็อกที่พบ", "발견 블록 수", "已找到区块", "Số khối tìm được")),
    # --- calculator ---
    ("Enter your gear's hashrate to see your real solo odds at the <b>current</b> network difficulty. Honest math — solo is a lottery, not a salary.",
     ("Masukkan hashrate perangkat Anda untuk melihat peluang solo nyata pada tingkat kesulitan jaringan <b>saat ini</b>. Matematika jujur — solo itu lotre, bukan gaji.",
      "機材の hashrate を入力すると、<b>現在の</b>ネットワーク難易度での本当のソロ確率がわかります。正直な計算 — ソロは宝くじであり給料ではありません。",
      "ใส่ hashrate ของอุปกรณ์เพื่อดูโอกาสโซโลจริงที่ความยากของเครือข่าย<b>ปัจจุบัน</b> คณิตศาสตร์ที่ซื่อสัตย์ — โซโลคือลอตเตอรี ไม่ใช่เงินเดือน",
      "장비의 hashrate를 입력하면 <b>현재</b> 네트워크 난이도 기준 실제 솔로 확률을 볼 수 있습니다. 정직한 계산 — 솔로는 복권이지 월급이 아닙니다.",
      "输入你设备的 hashrate，查看在<b>当前</b>全网难度下的真实单独挖矿概率。诚实的计算 — 单独挖矿是彩票，不是工资。",
      "Nhập hashrate thiết bị để xem xác suất solo thật ở độ khó mạng <b>hiện tại</b>. Tính toán trung thực — solo là xổ số, không phải lương.")),
    ("Expected time to your block", ("Perkiraan waktu untuk blok Anda", "ブロックまでの予想時間", "เวลาที่คาดว่าจะได้บล็อก", "블록까지 예상 시간", "预计挖到区块时间", "Thời gian dự kiến tới khối")),
    ("Your share of the network", ("Bagian Anda dari jaringan", "ネットワークに占める割合", "ส่วนแบ่งของคุณในเครือข่าย", "네트워크에서 당신의 비중", "你在全网的占比", "Tỷ lệ của bạn trong mạng")),
    ("Expected yield", ("Perkiraan hasil", "予想収益", "ผลตอบแทนที่คาดหวัง", "예상 수익", "预期收益", "Lợi nhuận dự kiến")),
    ("A block can land tomorrow or in a thousand years — the odds are the same every block. That's solo.",
     ("Sebuah blok bisa datang besok atau dalam seribu tahun — peluangnya sama setiap blok. Itulah solo.",
      "ブロックは明日来るかもしれないし千年後かもしれません — 確率は毎ブロック同じ。それがソロです。",
      "บล็อกอาจมาพรุ่งนี้หรืออีกพันปี — โอกาสเท่ากันทุกบล็อก นั่นแหละโซโล",
      "블록은 내일 나올 수도, 천 년 뒤에 나올 수도 있습니다 — 매 블록 확률은 같습니다. 그게 솔로입니다.",
      "区块可能明天出，也可能一千年后 — 每个区块的概率都一样。这就是单独挖矿。",
      "Một khối có thể đến ngày mai hoặc sau ngàn năm — xác suất mỗi khối là như nhau. Đó là solo.")),
    # --- why bullets ---
    ("Truly solo.", ("Benar-benar solo.", "完全にソロ。", "โซโลแท้จริง", "진정한 솔로.", "真正的单独挖矿。", "Solo thực sự.")),
    ("You mine to your own address. If you find a block, the whole reward is yours — minus a flat 2% fee.",
     ("Anda menambang ke alamat sendiri. Jika menemukan blok, seluruh hadiah jadi milik Anda — dikurangi biaya flat 2%.",
      "自分のアドレスで採掘します。ブロックを見つければ報酬は丸ごとあなたのもの — 一律2%の手数料を除いて。",
      "คุณขุดไปยังที่อยู่ของคุณเอง ถ้าเจอบล็อก รางวัลทั้งหมดเป็นของคุณ — หักค่าธรรมเนียมคงที่ 2%",
      "자신의 주소로 채굴합니다. 블록을 찾으면 보상 전체가 당신의 것입니다 — 일률 2% 수수료만 제외.",
      "你用自己的地址挖矿。挖到区块，全部奖励归你 — 仅扣固定 2% 费用。",
      "Bạn đào về địa chỉ của mình. Tìm được khối thì toàn bộ phần thưởng là của bạn — trừ phí cố định 2%.")),
    ("Non-custodial.", ("Non-kustodial.", "ノンカストディアル。", "ไม่เก็บรักษาเหรียญ", "비수탁.", "非托管。", "Không giữ hộ.")),
    ("We never hold your coins. There's nothing to withdraw and nothing for us to lose.",
     ("Kami tidak pernah memegang koin Anda. Tidak ada yang perlu ditarik dan tidak ada yang bisa kami hilangkan.",
      "あなたのコインを預かりません。引き出すものも、私たちが失うものもありません。",
      "เราไม่เคยถือเหรียญของคุณ ไม่มีอะไรต้องถอน และไม่มีอะไรให้เราทำหาย",
      "우리는 당신의 코인을 보관하지 않습니다. 출금할 것도, 우리가 잃을 것도 없습니다.",
      "我们从不保管你的币。没有什么可提现，也没有什么会被我们弄丢。",
      "Chúng tôi không bao giờ giữ coin của bạn. Không có gì để rút và không có gì để chúng tôi làm mất.")),
    ("No account, no KYC.", ("Tanpa akun, tanpa KYC.", "アカウント・KYC不要。", "ไม่ต้องมีบัญชี ไม่ต้อง KYC", "계정·KYC 없음.", "无需账户，无需 KYC。", "Không tài khoản, không KYC.")),
    ("No sign-up, no email, no ID. Point a miner at us and you're in.",
     ("Tanpa pendaftaran, email, atau identitas. Arahkan miner ke kami dan Anda langsung masuk.",
      "登録・メール・身分証は不要。マイナーを向けるだけで参加できます。",
      "ไม่ต้องสมัคร ไม่ต้องอีเมล ไม่ต้องบัตรประชาชน แค่ชี้เครื่องขุดมาที่เราก็เริ่มได้เลย",
      "가입·이메일·신분증 필요 없음. 채굴기를 우리에게 연결하면 끝입니다.",
      "无需注册、邮箱、身份证。把矿机指向我们即可加入。",
      "Không đăng ký, không email, không giấy tờ. Trỏ máy đào vào chúng tôi là xong.")),
    ("For the Asian mining community.", ("Untuk komunitas penambang Asia.", "アジアのマイニングコミュニティのために。", "เพื่อชุมชนนักขุดเอเชีย", "아시아 채굴 커뮤니티를 위해.", "为亚洲挖矿社区而建。", "Cho cộng đồng đào coin châu Á.")),
    ("A well-connected node close to home means your blocks reach the network fast.",
     ("Node yang terhubung baik dan dekat dengan rumah membuat blok Anda cepat sampai ke jaringan.",
      "近くにある接続性の高いノードのおかげで、あなたのブロックは素早くネットワークに届きます。",
      "โหนดที่เชื่อมต่อดีและอยู่ใกล้บ้าน ทำให้บล็อกของคุณถึงเครือข่ายได้เร็ว",
      "가까운 곳의 연결성 좋은 노드 덕분에 당신의 블록이 네트워크에 빠르게 도달합니다.",
      "靠近本地、连接良好的节点让你的区块快速送达全网。",
      "Node kết nối tốt và gần nhà giúp khối của bạn đến mạng nhanh.")),
    ("Transparent by default.", ("Transparan secara default.", "デフォルトで透明。", "โปร่งใสโดยพื้นฐาน", "기본이 투명.", "默认透明。", "Minh bạch mặc định.")),
    ("The numbers on this page are the real ones — same hashrate, same odds, same blocks we see.",
     ("Angka di halaman ini adalah yang asli — hashrate, peluang, dan blok yang sama dengan yang kami lihat.",
      "このページの数字は本物です — 私たちが見ているのと同じ hashrate、確率、ブロックです。",
      "ตัวเลขในหน้านี้คือของจริง — hashrate โอกาส และบล็อกเดียวกับที่เราเห็น",
      "이 페이지의 숫자는 진짜입니다 — 우리가 보는 것과 같은 hashrate, 확률, 블록입니다.",
      "本页的数字都是真实的 — 与我们看到的 hashrate、概率、区块完全一致。",
      "Các con số trên trang này là thật — cùng hashrate, cùng xác suất, cùng khối mà chúng tôi thấy.")),
    # --- track / cta ---
    ("Track any address — yours or anyone's. Nothing here is hidden behind a login.",
     ("Lacak alamat mana pun — milik Anda atau siapa saja. Tidak ada yang disembunyikan di balik login.",
      "どのアドレスでも追跡できます — 自分のものでも他人のものでも。ログインの裏に隠したものはありません。",
      "ติดตามที่อยู่ใดก็ได้ — ของคุณหรือของใครก็ตาม ไม่มีอะไรซ่อนหลังการล็อกอิน",
      "어떤 주소든 추적하세요 — 당신 것이든 누구 것이든. 로그인 뒤에 숨긴 것은 없습니다.",
      "追踪任意地址 — 你的或任何人的。这里没有任何东西藏在登录之后。",
      "Theo dõi bất kỳ địa chỉ nào — của bạn hay của ai khác. Không có gì giấu sau đăng nhập.")),
    ("View stats", ("Lihat statistik", "統計を見る", "ดูสถิติ", "통계 보기", "查看统计", "Xem thống kê")),
    # --- FAQ questions ---
    ("What is solo mining?", ("Apa itu menambang solo?", "ソロマイニングとは？", "การขุดโซโลคืออะไร?", "솔로 채굴이란?", "什么是单独挖矿？", "Đào solo là gì?")),
    ("Why SoloLuck instead of going solo at home?", ("Kenapa SoloLuck dibanding solo sendiri di rumah?", "自宅でソロをやる代わりに、なぜ SoloLuck？", "ทำไมต้อง SoloLuck แทนที่จะขุดโซโลเองที่บ้าน?", "집에서 직접 솔로 하는 대신 왜 SoloLuck인가요?", "为什么用 SoloLuck 而不是在家自己单独挖？", "Vì sao dùng SoloLuck thay vì tự solo ở nhà?")),
    ("How does the 2% fee work?", ("Bagaimana biaya 2% bekerja?", "2%の手数料はどう機能しますか？", "ค่าธรรมเนียม 2% ทำงานอย่างไร?", "2% 수수료는 어떻게 작동하나요?", "2% 费用如何运作？", "Phí 2% hoạt động thế nào?")),
    ("What username / password do I use?", ("Username / password apa yang saya pakai?", "ユーザー名・パスワードは何を使う？", "ใช้ username / password อะไร?", "어떤 username / password를 쓰나요?", "用什么用户名 / 密码？", "Dùng username / password nào?")),
    ("What hardware works?", ("Perangkat apa yang bisa dipakai?", "どのハードウェアが使えますか？", "ฮาร์ดแวร์อะไรใช้ได้บ้าง?", "어떤 하드웨어가 작동하나요?", "支持什么硬件？", "Phần cứng nào dùng được?")),
    ("Is it safe and non-custodial?", ("Apakah aman dan non-kustodial?", "安全でノンカストディアルですか？", "ปลอดภัยและไม่เก็บรักษาเหรียญหรือไม่?", "안전하고 비수탁인가요?", "安全且非托管吗？", "Có an toàn và không giữ hộ không?")),
    ("When and how do I get paid?", ("Kapan dan bagaimana saya dibayar?", "いつ・どのように支払われますか？", "ได้รับเงินเมื่อไรและอย่างไร?", "언제 어떻게 지급받나요?", "何时、如何收款？", "Khi nào và làm sao tôi được trả?")),
    # --- rules ---
    ("One address = your payout identity. Use <code>address.workername</code> to track multiple rigs.",
     ("Satu alamat = identitas pembayaran Anda. Gunakan <code>address.workername</code> untuk melacak banyak rig.",
      "1つのアドレス＝あなたの支払い identity。複数のリグは <code>address.workername</code> で管理します。",
      "หนึ่งที่อยู่ = ตัวตนรับเงินของคุณ ใช้ <code>address.workername</code> เพื่อติดตามหลายเครื่อง",
      "하나의 주소 = 당신의 지급 identity. 여러 장비는 <code>address.workername</code>으로 추적하세요.",
      "一个地址 = 你的收款身份。多台矿机用 <code>address.workername</code> 区分追踪。",
      "Một địa chỉ = danh tính nhận tiền của bạn. Dùng <code>address.workername</code> để theo dõi nhiều giàn.")),
    ("Minimum ~100 GH/s recommended — below that the lottery odds round to zero.",
     ("Minimum ~100 GH/s disarankan — di bawah itu peluang lotre membulat ke nol.",
      "推奨は最低 ~100 GH/s — それ以下では当選確率がほぼ0に丸まります。",
      "แนะนำขั้นต่ำ ~100 GH/s — ต่ำกว่านั้นโอกาสจะปัดเป็นศูนย์",
      "建议最低 ~100 GH/s — 低于此值中奖概率趋近于零。",
      "권장 최소 ~100 GH/s — 그 이하는 당첨 확률이 0에 수렴합니다.",
      "Khuyến nghị tối thiểu ~100 GH/s — thấp hơn thì xác suất gần như bằng 0.")),
    ("Pick the port that matches your gear; vardiff handles the rest automatically.",
     ("Pilih port sesuai perangkat Anda; vardiff menangani sisanya otomatis.",
      "機材に合うポートを選べば、あとは vardiff が自動で調整します。",
      "เลือกพอร์ตที่ตรงกับอุปกรณ์ แล้ว vardiff จะจัดการที่เหลือให้อัตโนมัติ",
      "장비에 맞는 포트를 고르면 나머지는 vardiff가 자동 처리합니다.",
      "选择匹配你设备的端口，其余交给 vardiff 自动处理。",
      "Chọn cổng phù hợp thiết bị; vardiff lo phần còn lại tự động.")),
    ("Best-effort uptime, no guarantees. Solo mining is a fair lottery — we just sell you the ticket.",
     ("Uptime sebaik mungkin, tanpa jaminan. Menambang solo adalah lotre yang adil — kami hanya menjual tiketnya.",
      "ベストエフォートの稼働、保証なし。ソロは公平な宝くじ — 私たちはチケットを売るだけです。",
      "พยายามให้ออนไลน์ที่สุด แต่ไม่รับประกัน การขุดโซโลคือลอตเตอรีที่ยุติธรรม — เราแค่ขายตั๋วให้คุณ",
      "최선의 가동, 보장은 없음. 솔로는 공정한 복권입니다 — 우리는 티켓만 팔 뿐입니다.",
      "尽力保证在线，但不作担保。单独挖矿是公平的彩票 — 我们只是卖票给你。",
      "Cố gắng online tối đa, không bảo đảm. Solo là xổ số công bằng — chúng tôi chỉ bán vé.")),
    ("No cookies, no trackers, no third-party assets — this page loads nothing from anyone but us.",
     ("Tanpa cookie, pelacak, atau aset pihak ketiga — halaman ini tidak memuat apa pun selain dari kami.",
      "クッキー・トラッカー・サードパーティ資産なし — このページは私たち以外から何も読み込みません。",
      "ไม่มีคุกกี้ ไม่มีตัวติดตาม ไม่มีไฟล์จากภายนอก — หน้านี้โหลดเฉพาะจากเราเท่านั้น",
      "쿠키·트래커·서드파티 자산 없음 — 이 페이지는 우리 외에는 아무것도 불러오지 않습니다.",
      "无 cookie、无追踪器、无第三方资源 — 本页只从我们这里加载内容。",
      "Không cookie, không trình theo dõi, không tài nguyên bên thứ ba — trang này chỉ tải từ chúng tôi.")),
    # --- connect ---
    ("The port only sets your <b>starting</b> difficulty. Vardiff tunes it automatically afterward, so just pick the closest match.",
     ("Port hanya menentukan tingkat kesulitan <b>awal</b> Anda. Vardiff menyesuaikannya otomatis, jadi pilih saja yang paling mendekati.",
      "ポートは<b>開始</b>難易度を決めるだけです。あとは vardiff が自動調整するので、近いものを選べばOKです。",
      "พอร์ตกำหนดแค่ความยาก<b>เริ่มต้น</b> หลังจากนั้น vardiff ปรับให้อัตโนมัติ เลือกอันที่ใกล้ที่สุดพอ",
      "포트는 <b>시작</b> 난이도만 정합니다. 이후 vardiff가 자동 조정하니 가장 가까운 것을 고르세요.",
      "端口只决定你的<b>起始</b>难度。之后 vardiff 会自动调整，选最接近的即可。",
      "Cổng chỉ đặt độ khó <b>ban đầu</b>. Sau đó vardiff tự chỉnh, cứ chọn cái gần nhất.")),
    ("START HERE", ("MULAI DI SINI", "ここから", "เริ่มที่นี่", "여기서 시작", "从这里开始", "BẮT ĐẦU TẠI ĐÂY")),
    ("Tiny &amp; hobby rigs, &lt; ~2 TH/s", ("Rig kecil &amp; hobi, &lt; ~2 TH/s", "小型・趣味リグ、&lt; ~2 TH/s", "เครื่องเล็ก &amp; งานอดิเรก &lt; ~2 TH/s", "소형 &amp; 취미 장비, &lt; ~2 TH/s", "小型 &amp; 业余设备，&lt; ~2 TH/s", "Giàn nhỏ &amp; nghiệp dư, &lt; ~2 TH/s")),
    ("Home &amp; small-farm ASICs, ~2–200 TH/s", ("ASIC rumahan &amp; farm kecil, ~2–200 TH/s", "家庭・小規模ファームの ASIC、~2–200 TH/s", "ASIC ที่บ้าน &amp; ฟาร์มเล็ก ~2–200 TH/s", "가정 &amp; 소규모 팜 ASIC, ~2–200 TH/s", "家用 &amp; 小型矿场 ASIC，~2–200 TH/s", "ASIC gia đình &amp; trại nhỏ, ~2–200 TH/s")),
    ("Modern &amp; clustered ASICs, 200 TH/s+", ("ASIC modern &amp; klaster, 200 TH/s+", "最新・クラスタ ASIC、200 TH/s+", "ASIC รุ่นใหม่ &amp; แบบคลัสเตอร์ 200 TH/s+", "최신 &amp; 클러스터 ASIC, 200 TH/s+", "现代 &amp; 集群 ASIC，200 TH/s+", "ASIC hiện đại &amp; cụm, 200 TH/s+")),
    ("Start difficulty", ("Kesulitan awal", "開始難易度", "ความยากเริ่มต้น", "시작 난이도", "起始难度", "Độ khó ban đầu")),
    ("How to log in.", ("Cara login.", "ログイン方法。", "วิธีล็อกอิน", "로그인 방법.", "如何登录。", "Cách đăng nhập.")),
    # --- footer / status ---
    (" miners\n", (" penambang\n", " マイナー\n", " นักขุด\n", " 채굴자\n", " 矿工\n", " thợ đào\n")),
    ("these are the real, unedited pool numbers.", ("ini angka pool yang asli dan tidak diedit.", "これは編集していない本物のプール数値です。", "นี่คือตัวเลขพูลจริงที่ไม่ได้แก้ไข", "편집하지 않은 진짜 풀 수치입니다.", "这些是未经编辑的真实矿池数据。", "đây là số liệu pool thật, chưa chỉnh sửa.")),
    ("No blocks solved yet — be the first.", ("Belum ada blok yang ditemukan — jadilah yang pertama.", "まだブロックはありません — 最初の一人になりましょう。", "ยังไม่มีบล็อกที่ขุดได้ — มาเป็นคนแรกกัน", "아직 발견한 블록 없음 — 첫 번째가 되세요.", "尚未挖到区块 — 来做第一个吧。", "Chưa có khối nào — hãy là người đầu tiên.")),
    # --- per-address hashrate chart (SSR sparkline on /users) ---
    ("Your hashrate · last 24h", ("Hashrate Anda · 24 jam terakhir", "あなたのハッシュレート · 直近24時間", "แฮชเรตของคุณ · 24 ชม.ล่าสุด", "내 해시레이트 · 최근 24시간", "您的算力 · 最近24小时", "Hashrate của bạn · 24 giờ qua")),
    ("10-minute averages, your workers combined. Updates as you mine.", ("Rata-rata 10 menit, gabungan worker Anda. Diperbarui saat Anda menambang.", "10分平均、全ワーカー合算。採掘中に更新されます。", "ค่าเฉลี่ย 10 นาที รวมผู้ปฏิบัติงานของคุณ อัปเดตขณะที่คุณขุด", "10분 평균, 모든 워커 합산. 채굴하는 동안 업데이트됩니다.", "10分钟平均，合并您的所有矿机。挖矿时持续更新。", "Trung bình 10 phút, gộp các worker của bạn. Cập nhật khi bạn đào.")),
    ("History is building — check back after a bit of mining.", ("Riwayat sedang terbentuk — cek lagi setelah menambang sebentar.", "履歴を蓄積中です — 少し採掘してから再度ご確認ください。", "กำลังสะสมประวัติ — กลับมาดูอีกครั้งหลังขุดสักพัก", "기록을 쌓는 중입니다 — 잠시 채굴 후 다시 확인하세요.", "正在积累历史数据 — 挖矿一段时间后再回来查看。", "Đang tích lũy lịch sử — quay lại sau khi đào một lúc.")),
]
TR = [(en, dict(zip(_LO, vals))) for en, vals in _TR_RAW]
# Additional Asian languages — Malay (ms), Filipino (tl), Hindi (hi).
# Keyed by the SAME English phrases as _TR_RAW so they merge into TR. First-pass
# translations (crypto terms kept in English); worth a native review per language.
_TR_ADD = {
 "Asia&#x27;s community solo Bitcoin pool": {"ms":"Pool solo Bitcoin komuniti Asia","tl":"Komunidad na solo Bitcoin pool ng Asya","hi":"एशिया का कम्युनिटी सोलो Bitcoin पूल"},
 "Asia's community solo Bitcoin pool": {"ms":"Pool solo Bitcoin komuniti Asia","tl":"Komunidad na solo Bitcoin pool ng Asya","hi":"एशिया का कम्युनिटी सोलो Bitcoin पूल"},
 POOL_PITCH: {
   "ms":"Lombong ke alamat anda sendiri. Jumpa satu blok dan seluruh ganjaran jadi milik anda — ditolak yuran tetap 2%, dibayar terus kepada anda secara on-chain. Bukan kustodi: tiada akaun, tiada KYC, kami tidak pernah memegang koin anda.",
   "tl":"Magmina sa sarili mong address. Kapag may nahanap kang block, sa'yo ang buong reward — bawas lang ang flat 2% fee, diretsong bayad sa'yo on-chain. Non-custodial: walang account, walang KYC, hindi namin hawak ang coins mo.",
   "hi":"अपने ही address पर माइन करें। एक block मिला तो पूरा reward आपका — सिर्फ़ 2% फ़्लैट fee घटाकर, सीधे आपको on-chain मिलता है। Non-custodial: कोई account नहीं, कोई KYC नहीं, हम आपके coins कभी नहीं रखते।"},
 "No account. No KYC. No custodian — your address is your payout. Honest stats, real odds, real blocks.": {
   "ms":"Tiada akaun. Tiada KYC. Tiada kustodi — alamat anda ialah bayaran anda. Statistik jujur, peluang sebenar, blok sebenar.",
   "tl":"Walang account. Walang KYC. Walang custodian — ang address mo ang payout mo. Totoong stats, totoong odds, totoong blocks.",
   "hi":"कोई account नहीं। कोई KYC नहीं। कोई custodian नहीं — आपका address ही आपका payout है। ईमानदार आँकड़े, असली odds, असली blocks।"},
 "Live pool stats": {"ms":"Statistik pool langsung","tl":"Live na stats ng pool","hi":"लाइव पूल आँकड़े"},
 "Solo odds calculator": {"ms":"Kalkulator peluang solo","tl":"Solo odds calculator","hi":"सोलो संभावना कैलकुलेटर"},
 "Why SoloLuck": {"ms":"Kenapa SoloLuck","tl":"Bakit SoloLuck","hi":"SoloLuck क्यों"},
 "Track an address": {"ms":"Jejak alamat","tl":"Mag-track ng address","hi":"कोई address ट्रैक करें"},
 "The fee — flat 2%, nothing hidden": {"ms":"Yuran — tetap 2%, tiada yang tersembunyi","tl":"Ang fee — flat 2%, walang tago","hi":"फ़ीस — फ़्लैट 2%, कुछ भी छिपा नहीं"},
 "Found Blocks": {"ms":"Blok Dijumpai","tl":"Mga Nahanap na Block","hi":"मिले हुए Blocks"},
 "Rules &amp; guidance": {"ms":"Peraturan &amp; panduan","tl":"Mga patakaran &amp; gabay","hi":"नियम &amp; मार्गदर्शन"},
 "Connect — pick the port that matches your gear.": {"ms":"Sambung — pilih port yang sepadan dengan peranti anda.","tl":"Kumonekta — piliin ang port na bagay sa gear mo.","hi":"कनेक्ट करें — अपने gear से मेल खाता port चुनें।"},
 "Miners online": {"ms":"Pelombong dalam talian","tl":"Mga miner online","hi":"ऑनलाइन miners"},
 "Best share ever": {"ms":"Share terbaik","tl":"Pinakamataas na share","hi":"अब तक का best share"},
 "Network hashrate": {"ms":"Hashrate rangkaian","tl":"Network hashrate","hi":"नेटवर्क hashrate"},
 "Blocks found": {"ms":"Blok dijumpai","tl":"Mga block na nahanap","hi":"मिले Blocks"},
 "Enter your gear's hashrate to see your real solo odds at the <b>current</b> network difficulty. Honest math — solo is a lottery, not a salary.": {
   "ms":"Masukkan hashrate peranti anda untuk melihat peluang solo sebenar pada kesukaran rangkaian <b>semasa</b>. Matematik jujur — solo ialah loteri, bukan gaji.",
   "tl":"Ilagay ang hashrate ng gear mo para makita ang totoong solo odds sa <b>kasalukuyang</b> network difficulty. Tapat na math — lottery ang solo, hindi sweldo.",
   "hi":"<b>मौजूदा</b> network difficulty पर अपनी असली सोलो odds देखने के लिए अपने gear का hashrate डालें। ईमानदार गणित — सोलो एक lottery है, salary नहीं।"},
 "Expected time to your block": {"ms":"Anggaran masa untuk blok anda","tl":"Inaasahang oras para sa block mo","hi":"आपके block तक अनुमानित समय"},
 "Your share of the network": {"ms":"Bahagian anda daripada rangkaian","tl":"Ang share mo sa network","hi":"नेटवर्क में आपका हिस्सा"},
 "Expected yield": {"ms":"Anggaran hasil","tl":"Inaasahang kita","hi":"अनुमानित यील्ड"},
 "A block can land tomorrow or in a thousand years — the odds are the same every block. That's solo.": {
   "ms":"Satu blok boleh datang esok atau dalam seribu tahun — peluangnya sama setiap blok. Itulah solo.",
   "tl":"Pwedeng dumating ang block bukas o sa loob ng isang libong taon — pareho lang ang odds kada block. Ganyan ang solo.",
   "hi":"Block कल भी मिल सकता है या हज़ार साल में — हर block पर odds एक जैसे हैं। यही सोलो है।"},
 "Truly solo.": {"ms":"Benar-benar solo.","tl":"Tunay na solo.","hi":"सच में सोलो।"},
 "You mine to your own address. If you find a block, the whole reward is yours — minus a flat 2% fee.": {
   "ms":"Anda melombong ke alamat sendiri. Jika jumpa blok, seluruh ganjaran milik anda — ditolak yuran tetap 2%.",
   "tl":"Nagmimina ka sa sarili mong address. Kapag may nahanap kang block, sa'yo ang buong reward — bawas lang ang flat 2% fee.",
   "hi":"आप अपने address पर माइन करते हैं। Block मिला तो पूरा reward आपका — सिर्फ़ 2% फ़्लैट fee घटाकर।"},
 "Non-custodial.": {"ms":"Bukan kustodi.","tl":"Non-custodial.","hi":"Non-custodial।"},
 "We never hold your coins. There's nothing to withdraw and nothing for us to lose.": {
   "ms":"Kami tidak pernah memegang koin anda. Tiada apa untuk dikeluarkan dan tiada apa untuk kami hilang.",
   "tl":"Hindi namin hawak ang coins mo. Walang i-withdraw at walang mawawala sa amin.",
   "hi":"हम आपके coins कभी नहीं रखते। न कुछ withdraw करना है, न हमारे पास खोने को कुछ है।"},
 "No account, no KYC.": {"ms":"Tiada akaun, tiada KYC.","tl":"Walang account, walang KYC.","hi":"कोई account नहीं, कोई KYC नहीं।"},
 "No sign-up, no email, no ID. Point a miner at us and you're in.": {
   "ms":"Tiada pendaftaran, e-mel, atau ID. Halakan pelombong ke arah kami dan anda terus masuk.",
   "tl":"Walang sign-up, email, o ID. Itutok ang miner sa amin at pasok ka na.",
   "hi":"कोई sign-up नहीं, email नहीं, ID नहीं। एक miner हमारी ओर लगाइए और आप शामिल।"},
 "For the Asian mining community.": {"ms":"Untuk komuniti perlombongan Asia.","tl":"Para sa Asian mining community.","hi":"एशियाई mining समुदाय के लिए।"},
 "A well-connected node close to home means your blocks reach the network fast.": {
   "ms":"Node yang terhubung baik dan dekat membuatkan blok anda cepat sampai ke rangkaian.",
   "tl":"Ang malapit at well-connected na node ay nangangahulugang mabilis na umaabot sa network ang blocks mo.",
   "hi":"पास का और अच्छी तरह जुड़ा node मतलब आपके blocks जल्दी network तक पहुँचते हैं।"},
 "Transparent by default.": {"ms":"Telus secara lalai.","tl":"Transparent bilang default.","hi":"डिफ़ॉल्ट रूप से पारदर्शी।"},
 "The numbers on this page are the real ones — same hashrate, same odds, same blocks we see.": {
   "ms":"Angka di halaman ini adalah yang sebenar — hashrate, peluang, dan blok yang sama seperti yang kami lihat.",
   "tl":"Totoo ang mga numero sa page na ito — parehong hashrate, odds, at blocks na nakikita namin.",
   "hi":"इस page के आँकड़े असली हैं — वही hashrate, वही odds, वही blocks जो हम देखते हैं।"},
 "Track any address — yours or anyone's. Nothing here is hidden behind a login.": {
   "ms":"Jejak mana-mana alamat — milik anda atau sesiapa. Tiada apa disembunyikan di sebalik log masuk.",
   "tl":"Mag-track ng kahit anong address — sa'yo o kahit kanino. Walang nakatago sa likod ng login.",
   "hi":"कोई भी address ट्रैक करें — आपका या किसी का भी। यहाँ कुछ भी login के पीछे छिपा नहीं है।"},
 "View stats": {"ms":"Lihat statistik","tl":"Tingnan ang stats","hi":"आँकड़े देखें"},
 "What is solo mining?": {"ms":"Apakah perlombongan solo?","tl":"Ano ang solo mining?","hi":"सोलो माइनिंग क्या है?"},
 "Why SoloLuck instead of going solo at home?": {"ms":"Kenapa SoloLuck berbanding solo sendiri di rumah?","tl":"Bakit SoloLuck imbes na solo sa bahay?","hi":"घर पर खुद सोलो करने के बजाय SoloLuck क्यों?"},
 "How does the 2% fee work?": {"ms":"Bagaimana yuran 2% berfungsi?","tl":"Paano gumagana ang 2% fee?","hi":"2% fee कैसे काम करती है?"},
 "What username / password do I use?": {"ms":"Username / password apa yang saya guna?","tl":"Anong username / password ang gagamitin ko?","hi":"मैं कौन-सा username / password इस्तेमाल करूँ?"},
 "What hardware works?": {"ms":"Perkakasan apa yang boleh digunakan?","tl":"Anong hardware ang pwede?","hi":"कौन-सा hardware चलेगा?"},
 "Is it safe and non-custodial?": {"ms":"Adakah ia selamat dan bukan kustodi?","tl":"Ligtas ba at non-custodial?","hi":"क्या यह सुरक्षित और non-custodial है?"},
 "When and how do I get paid?": {"ms":"Bila dan bagaimana saya dibayar?","tl":"Kailan at paano ako babayaran?","hi":"मुझे कब और कैसे payment मिलती है?"},
 "One address = your payout identity. Use <code>address.workername</code> to track multiple rigs.": {
   "ms":"Satu alamat = identiti bayaran anda. Guna <code>address.workername</code> untuk menjejak banyak rig.",
   "tl":"Isang address = ang payout identity mo. Gamitin ang <code>address.workername</code> para i-track ang maraming rig.",
   "hi":"एक address = आपकी payout पहचान। कई rigs ट्रैक करने के लिए <code>address.workername</code> इस्तेमाल करें।"},
 "Minimum ~100 GH/s recommended — below that the lottery odds round to zero.": {
   "ms":"Minimum ~100 GH/s disyorkan — di bawah itu peluang loteri menjadi sifar.",
   "tl":"Inirerekomenda ang minimum ~100 GH/s — pababa diyan, halos zero na ang odds.",
   "hi":"न्यूनतम ~100 GH/s सुझाया जाता है — उससे कम पर lottery odds शून्य हो जाते हैं।"},
 "Pick the port that matches your gear; vardiff handles the rest automatically.": {
   "ms":"Pilih port yang sepadan dengan peranti anda; vardiff uruskan selebihnya secara automatik.",
   "tl":"Piliin ang port na bagay sa gear mo; ang vardiff na ang bahala sa iba, automatic.",
   "hi":"अपने gear से मेल खाता port चुनें; बाक़ी vardiff अपने-आप संभाल लेता है।"},
 "Best-effort uptime, no guarantees. Solo mining is a fair lottery — we just sell you the ticket.": {
   "ms":"Uptime sebaik mungkin, tanpa jaminan. Perlombongan solo ialah loteri adil — kami hanya menjual tiket.",
   "tl":"Best-effort na uptime, walang garantiya. Patas na lottery ang solo mining — kami lang ang nagbebenta ng ticket.",
   "hi":"यथासंभव uptime, कोई गारंटी नहीं। सोलो माइनिंग एक निष्पक्ष lottery है — हम बस टिकट बेचते हैं।"},
 "No cookies, no trackers, no third-party assets — this page loads nothing from anyone but us.": {
   "ms":"Tiada kuki, penjejak, atau aset pihak ketiga — halaman ini tidak memuatkan apa-apa selain daripada kami.",
   "tl":"Walang cookies, trackers, o third-party assets — wala itong nilo-load mula sa iba kundi sa amin.",
   "hi":"कोई cookies नहीं, trackers नहीं, third-party assets नहीं — यह page हमारे सिवा कुछ भी load नहीं करता।"},
 "The port only sets your <b>starting</b> difficulty. Vardiff tunes it automatically afterward, so just pick the closest match.": {
   "ms":"Port hanya menetapkan kesukaran <b>permulaan</b> anda. Vardiff melaras secara automatik selepas itu, jadi pilih yang paling hampir.",
   "tl":"Itinatakda lang ng port ang <b>panimulang</b> difficulty mo. Awtomatikong ina-adjust ito ng vardiff pagkatapos, kaya piliin lang ang pinakamalapit.",
   "hi":"Port सिर्फ़ आपकी <b>शुरुआती</b> difficulty तय करता है। उसके बाद vardiff अपने-आप समायोजित करता है, बस सबसे क़रीबी चुनें।"},
 "START HERE": {"ms":"MULA DI SINI","tl":"MAGSIMULA DITO","hi":"यहाँ से शुरू करें"},
 "Tiny &amp; hobby rigs, &lt; ~2 TH/s": {"ms":"Rig kecil &amp; hobi, &lt; ~2 TH/s","tl":"Maliit &amp; hobby rigs, &lt; ~2 TH/s","hi":"छोटे &amp; hobby rigs, &lt; ~2 TH/s"},
 "Home &amp; small-farm ASICs, ~2–200 TH/s": {"ms":"ASIC rumah &amp; ladang kecil, ~2–200 TH/s","tl":"Home &amp; small-farm ASICs, ~2–200 TH/s","hi":"घरेलू &amp; small-farm ASICs, ~2–200 TH/s"},
 "Modern &amp; clustered ASICs, 200 TH/s+": {"ms":"ASIC moden &amp; berkelompok, 200 TH/s+","tl":"Modern &amp; clustered ASICs, 200 TH/s+","hi":"आधुनिक &amp; clustered ASICs, 200 TH/s+"},
 "Start difficulty": {"ms":"Kesukaran permulaan","tl":"Panimulang difficulty","hi":"शुरुआती difficulty"},
 "How to log in.": {"ms":"Cara log masuk.","tl":"Paano mag-login.","hi":"लॉगिन कैसे करें।"},
 " miners\n": {"ms":" pelombong\n","tl":" miners\n","hi":" miners\n"},
 "these are the real, unedited pool numbers.": {"ms":"ini angka pool sebenar yang tidak disunting.","tl":"ito ang totoo at hindi inedit na pool numbers.","hi":"ये असली, बिना संपादित pool आँकड़े हैं।"},
 "No blocks solved yet — be the first.": {"ms":"Belum ada blok dijumpai — jadilah yang pertama.","tl":"Wala pang nahanap na block — ikaw ang mauna.","hi":"अभी तक कोई block नहीं मिला — पहले बनें।"},
 "Your hashrate · last 24h": {"ms":"Hashrate anda · 24 jam lalu","tl":"Hashrate mo · huling 24h","hi":"आपका hashrate · पिछले 24 घंटे"},
 "10-minute averages, your workers combined. Updates as you mine.": {"ms":"Purata 10 minit, gabungan worker anda. Dikemas kini semasa anda melombong.","tl":"10-minutong average, pinagsama ang mga worker mo. Nag-a-update habang nagmimina ka.","hi":"10-मिनट औसत, आपके सभी workers मिलाकर। माइन करते समय अपडेट होता है।"},
 "History is building — check back after a bit of mining.": {"ms":"Sejarah sedang terbina — semak semula selepas melombong sebentar.","tl":"Bumubuo pa ng history — balik ka mamaya pagkatapos magmina nang kaunti.","hi":"इतिहास बन रहा है — थोड़ा माइन करने के बाद वापस देखें।"},
}
for _en, _d in TR:
    if _en in _TR_ADD:
        _d.update(_TR_ADD[_en])


def _translate(page, lang):
    if lang == "en":
        return page
    out = page.replace('<html lang="en">', '<html lang="%s">' % lang, 1)
    # Apply longest keys first: an ordered str.replace lets a short key (e.g.
    # "Why SoloLuck") consume the prefix of a longer phrase ("Why SoloLuck
    # instead of going solo at home?") and leave the rest in English. Sorting by
    # key length descending inoculates against all such prefix collisions.
    for en, d in sorted(TR, key=lambda kv: len(kv[0]), reverse=True):
        t = d.get(lang)
        if t:
            out = out.replace(en, t)
    return out


def _lang_toggle(lang):
    cur = lang if lang in SUPPORTED_LANGS else "en"
    out = []
    for code, flag, name in LANG_BTNS:
        on = ' class="on"' if code == cur else ""
        out.append('<a href="?lang=%s"%s title="%s" aria-label="%s">%s</a>'
                   % (code, on, name, name, flag))
    return "".join(out)


def render_landing(lang="en"):
    v = build_public_view()
    fee = v["fee_pct"]

    # blocks section
    if v["blocks"]:
        items = []
        for b in v["blocks"]:
            h = ("#%s" % b["height"]) if b.get("height") else "block"
            full = b.get("hash") or ""
            # link the block to a public explorer -> a found block is the pool's
            # make-or-break proof; make it one click from on-chain verification.
            label = html.escape(h)
            if full:
                label = ('<a href="https://mempool.space/block/%s" target="_blank" '
                         'rel="noopener">%s</a>' % (html.escape(full), label))
            hash_short = (full[:16] + "…") if full else ""
            when = html.escape(str(b.get("when") or ""))
            items.append("<li>%s <span class='muted'>%s %s</span></li>"
                         % (label, html.escape(hash_short), when))
        blocks_html = "<ul class='blocks'>%s</ul>" % "".join(items)
    else:
        blocks_html = "<p class='muted'>No blocks solved yet — be the first.</p>"

    online = v["online"]
    dot_cls = "dot" if online else "dot off"
    live_word = "LIVE" if online else "OFFLINE"
    hr1m = str(v["hashrate"]["1m"] or "—")
    workers = v["workers"] if v["workers"] is not None else "—"
    hi = _head_i18n(lang)

    page = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SoloLuck — Asia's community solo Bitcoin pool</title>
<meta name="description" content="%(desc)s">
<meta name="keywords" content="solo mining pool, Bitcoin solo pool, Asia mining pool, solo ckpool, true solo Bitcoin, non-custodial mining, Indonesia Bitcoin pool, Bitaxe NerdQAxe pool">
<meta name="theme-color" content="#0b0e14">
<link rel="canonical" href="%(canon)s">%(hreflang)s
<meta property="og:type" content="website">
<meta property="og:site_name" content="SoloLuck">
<meta property="og:title" content="SoloLuck — Asia's community solo Bitcoin pool">
<meta property="og:description" content="%(desc)s">
<meta property="og:url" content="%(canon)s">
<meta property="og:locale" content="%(oglocale)s">%(oglocale_alt)s
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="SoloLuck — Asia's community solo Bitcoin pool">
<meta name="twitter:description" content="%(desc)s">
<meta property="og:image" content="https://sololuck.io/og.png">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:image" content="https://sololuck.io/og.png">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>%(css)s</style>%(jsonld)s</head><body>
<div class="wrap">
<div class="langtoggle">%(langtoggle)s</div>

<header>
  <h1 class="brand"><span class="b">Solo</span>Luck</h1>
  <p class="tagline">%(tagline)s</p>
  <p class="pitch">%(pitch)s</p>
  <p class="statusbar">
    <span class="%(dot_cls)s" id="livedot"></span><span class="hl" id="liveword">%(live_word)s</span>
    · <span class="hl" id="hr1m">%(hr1m)s</span>
    · <span id="workers">%(workers)s</span> miners
    <span class="muted">· solo · 2%% fee · paid on-chain to you</span>
  </p>
  <p class="trust">No account. No KYC. No custodian — your address is your payout. Honest stats, real odds, real blocks.</p>
  <p style="margin:16px 0 0"><a class="btn cta" href="#connect">Start mining — get your stratum URL</a></p>
</header>

<div class="card">
  <h2>Live pool stats</h2>
  <div class="grid">
    <div class="stat"><div class="k">Hashrate · 1m</div><div class="v" id="s_hr1m">%(hr1m)s</div></div>
    <div class="stat"><div class="k">Hashrate · 5m</div><div class="v" id="s_hr5m">%(hr5m)s</div></div>
    <div class="stat"><div class="k">Hashrate · 1h</div><div class="v" id="s_hr1h">%(hr1h)s</div></div>
    <div class="stat"><div class="k">Hashrate · 1d</div><div class="v" id="s_hr1d">%(hr1d)s</div></div>
    <div class="stat"><div class="k">Miners online</div><div class="v" id="s_workers">%(workers)s</div></div>
    <div class="stat"><div class="k">Best share ever</div><div class="v" id="s_best">%(best)s</div><div class="sub" id="s_best_sub"></div></div>
    <div class="stat"><div class="k">Network hashrate</div><div class="v" id="s_nethash">%(nethash)s</div></div>
    <div class="stat"><div class="k">Blocks found</div><div class="v" id="s_blocks">%(blocksfound)s</div></div>
  </div>
  <p class="muted" id="updated" style="margin-top:12px">updated %(updated)s · these are the real, unedited pool numbers.</p>
</div>

<div class="card">
  <h2>Pool hashrate</h2>
  <p class="muted">Live pool-wide hashrate &middot; 10-minute buckets with a 1-hour average &middot; last 24h.</p>
  <div class="hrwrap">
    <svg id="hrSvg" viewBox="0 0 920 320" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Pool hashrate over the last 24 hours">
      <defs><linearGradient id="hrGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#f7931a" stop-opacity="0.32"/>
        <stop offset="0.65" stop-color="#f7931a" stop-opacity="0.09"/>
        <stop offset="1" stop-color="#f7931a" stop-opacity="0"/>
      </linearGradient></defs>
      <g class="hrgrid" id="hrGrid"></g>
      <path id="hrArea" fill="url(#hrGrad)" stroke="none"></path>
      <path id="hrLine" fill="none" stroke="#f7931a" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></path>
      <path id="hrMean" fill="none" stroke="#ffc46b" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"></path>
      <g class="hraxis" id="hrAxis"></g>
      <g id="hrHover" style="display:none">
        <line id="hrCross" y1="14" y2="292" stroke="#43506b" stroke-width="1" stroke-dasharray="3 3"></line>
        <circle id="hrDot" r="4" fill="#f7931a" stroke="#0b0e14" stroke-width="1.5"></circle>
      </g>
    </svg>
    <div id="hrTip" class="hr-tip" style="display:none"></div>
    <div id="hrEmpty" class="hr-empty">collecting hashrate history&hellip;</div>
  </div>
  <div class="hr-legend">
    <span><i class="sw" style="background:#f7931a"></i>10-minute</span>
    <span><i class="sw" style="background:#ffc46b"></i>1-hour average</span>
    <span class="muted" id="hrMeta"></span>
  </div>
</div>

<div class="card">
  <h2>Solo odds calculator</h2>
  <p class="muted">Enter your gear's hashrate to see your real solo odds at the <b>current</b> network difficulty. Honest math — solo is a lottery, not a salary.</p>
  <form class="lookform" onsubmit="return false" style="margin-bottom:14px">
    <input id="calcHr" type="number" min="0" step="any" inputmode="decimal" placeholder="e.g. 100" style="max-width:170px">
    <select id="calcUnit" class="btn" style="cursor:pointer;padding:10px">
      <option value="1000000000">GH/s</option>
      <option value="1000000000000" selected>TH/s</option>
      <option value="1000000000000000">PH/s</option>
    </select>
  </form>
  <div class="grid">
    <div class="stat"><div class="k">Expected time to your block</div><div class="v" id="calcEta">—</div></div>
    <div class="stat"><div class="k">Your share of the network</div><div class="v" id="calcShare">—</div></div>
    <div class="stat"><div class="k">Expected yield</div><div class="v" id="calcYield">—</div></div>
  </div>
  <p class="muted" style="margin-top:10px">A block can land tomorrow or in a thousand years — the odds are the same every block. That's solo.</p>
</div>

%(connect)s

<div class="card">
  <h2>Why SoloLuck</h2>
  <ul class="bullets">
    <li><b>Truly solo.</b> You mine to your own address. If you find a block, the whole reward is yours — minus a flat 2%% fee.</li>
    <li><b>Non-custodial.</b> We never hold your coins. There's nothing to withdraw and nothing for us to lose.</li>
    <li><b>No account, no KYC.</b> No sign-up, no email, no ID. Point a miner at us and you're in.</li>
    <li><b>For the Asian mining community.</b> A well-connected node close to home means your blocks reach the network fast.</li>
    <li><b>Transparent by default.</b> The numbers on this page are the real ones — same hashrate, same odds, same blocks we see.</li>
  </ul>
</div>

<div class="card">
  <h2>Track an address</h2>
  <p class="muted">Track any address — yours or anyone's. Nothing here is hidden behind a login.</p>
  <form class="lookform" onsubmit="goUser(event)">
    <input id="addrbox" placeholder="bc1q… your BTC address" autocomplete="off" spellcheck="false">
    <button class="btn" type="submit">View stats</button>
  </form>
</div>

<div class="card">
  <h2>The fee — flat 2%%, nothing hidden</h2>
  <div class="feebig">%(fee)s%%</div>
  <p>Find a block and you keep <b>%(rest)s%%</b>. Our 2%% is taken once, in the block's
  own coinbase — so the split happens on-chain, in the open. We never touch the rest,
  because we never hold your coins. You get paid only when <i>you</i> solve a block,
  and when you do, the reward lands straight at your address.</p>
  <p class="muted">Solo mining is a fair lottery — we just sell you the ticket and
  keep the line to the network fast. Best-effort uptime, no guarantees.</p>
</div>

<div class="card">
  <h2>Found Blocks</h2>
  %(blocks)s
</div>

<div class="card">
  <h2>FAQ</h2>
  <details><summary>What is solo mining?</summary><p class="muted">You mine for whole blocks on your own. No small steady payouts — but if your miner solves a block, the <b>entire</b> reward (~3.125 BTC + fees) is yours, paid straight to your address. A lottery with a very big prize.</p></details>
  <details><summary>Why SoloLuck instead of going solo at home?</summary><p class="muted">We keep a fast, well-connected node close to Asia, so a block you find reaches the network instantly (less orphan risk). You skip running and syncing your own node — just point your miner at us.</p></details>
  <details><summary>How does the 2%% fee work?</summary><p class="muted">Only if <i>you</i> solve a block. The 2%% is taken once, inside that block's own coinbase, on-chain and in the open. No block, no fee — and we never hold your coins.</p></details>
  <details><summary>What username / password do I use?</summary><p class="muted">Your own BTC address (bech32 <code>bc1q…</code>) as the username. Add <code>.workername</code> to track multiple rigs (e.g. <code>bc1q….rig1</code>). The password can be anything.</p></details>
  <details><summary>What hardware works?</summary><p class="muted">Any SHA-256 ASIC — Bitaxe, NerdQAxe, Avalon, Antminer and the like. Pick the port that matches your hashrate; vardiff tunes the rest. ~100 GH/s is a sensible minimum.</p></details>
  <details><summary>Is it safe and non-custodial?</summary><p class="muted">Yes. We never hold your coins — no balance, no withdrawal. A found block pays directly to the address you mine with. No account, no KYC, no trackers.</p></details>
  <details><summary>When and how do I get paid?</summary><p class="muted">The instant you solve a block, the network pays its coinbase straight to your address. That's the only payout — solo is all-or-nothing.</p></details>
</div>

<div class="card">
  <h2>Rules &amp; guidance</h2>
  <ul class="bullets">
    <li>One address = your payout identity. Use <code>address.workername</code> to track multiple rigs.</li>
    <li>Minimum ~100 GH/s recommended — below that the lottery odds round to zero.</li>
    <li>Pick the port that matches your gear; vardiff handles the rest automatically.</li>
    <li>Please keep CPU / GPU / nerdminer toys on the Lite port — the heavier tiers are for real hardware.</li>
    <li>Best-effort uptime, no guarantees. Solo mining is a fair lottery — we just sell you the ticket.</li>
    <li>No cookies, no trackers, no third-party assets — this page loads nothing from anyone but us.</li>
  </ul>
</div>

<footer>SoloLuck · Asia's community solo Bitcoin pool · non-custodial · 2%% fee</footer>
</div>
<script>
function fmt(x){return (x===null||x===undefined||x==='')?'—':x;}
function grp(n){if(n===null||n===undefined||n==='')return '—';
  var v=Number(n);return isNaN(v)?String(n):v.toLocaleString('en-US');}
function goUser(e){e.preventDefault();
  var a=document.getElementById('addrbox').value.trim();
  if(a){var m=location.search.match(/lang=([a-z]{2})/);
    location.href='/users/'+encodeURIComponent(a)+(m?'?lang='+m[1]:'');}return false;}
function copyText(t,btn){
  function done(){var o=btn.textContent;btn.textContent='copied';
    setTimeout(function(){btn.textContent=o;},1500);}
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(done,function(){fallback(t);done();});
  }else{fallback(t);done();}
}
function fallback(t){try{var ta=document.createElement('textarea');ta.value=t;
  ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);
  ta.focus();ta.select();document.execCommand('copy');document.body.removeChild(ta);
}catch(e){}}
document.addEventListener('click',function(e){
  var b=e.target.closest('.copy');
  if(b&&!b.disabled&&b.getAttribute('data-copy')){copyText(b.getAttribute('data-copy'),b);}
});
var NET={diff:null,hashrate:null,subsidy:null};
function cDur(s){if(s==null||!isFinite(s)||s<=0)return '—';var y=s/31557600;
  if(y>=1){if(y>=1e6)return '≈ '+(y/1e6).toFixed(1)+' million yr';if(y>=1e3)return '≈ '+(y/1e3).toFixed(1)+'k yr';return '≈ '+y.toFixed(0)+' yr';}
  var d=s/86400;if(d>=1)return d.toFixed(0)+' d';return (s/3600).toFixed(0)+' h';}
function cWords(n){n=Number(n)||0;if(n>=1e12)return (n/1e12).toFixed(1)+' trillion';if(n>=1e9)return (n/1e9).toFixed(1)+' billion';if(n>=1e6)return (n/1e6).toFixed(1)+' million';if(n>=1e3)return (n/1e3).toFixed(0)+' thousand';return n.toFixed(0);}
function calc(){
  var hrEl=document.getElementById('calcHr');if(!hrEl)return;
  var hr=parseFloat(hrEl.value)*parseFloat(document.getElementById('calcUnit').value);
  var e=document.getElementById('calcEta'),s=document.getElementById('calcShare'),y=document.getElementById('calcYield');
  if(!hr||hr<=0||!NET.diff){e.textContent='—';s.textContent='—';y.textContent='—';return;}
  var sec=NET.diff*Math.pow(2,32)/hr;
  e.textContent=cDur(sec);
  s.textContent=NET.hashrate?('1 in '+cWords(NET.hashrate/hr)):'—';
  if(NET.subsidy){var sats=NET.subsidy*86400/sec*1e8;y.textContent=(sats>=1?Math.round(sats).toLocaleString('en-US')+' sats/day':(sats*30).toFixed(0)+' sats/mo');}else{y.textContent='—';}
}
// ---- pool hashrate chart (hand-rolled inline SVG, no libraries) ----
var HRDATA=[];
function hsuf(v){v=Number(v)||0;if(v<=0)return '0 H/s';
  var u=[' H/s',' KH/s',' MH/s',' GH/s',' TH/s',' PH/s',' EH/s'];
  var p=Math.floor(Math.log10(v)/3);if(p<0)p=0;if(p>6)p=6;
  return (v/Math.pow(1000,p)).toFixed(1)+u[p];}
function niceMax(v){if(!(v>0))return 1;
  var mag=Math.pow(10,Math.floor(Math.log10(v)));var n=v/mag;
  var s=n<=1?1:n<=2?2:n<=5?5:10;return s*mag;}
function smoothPath(pts){
  if(!pts.length)return '';
  if(pts.length<3)return 'M'+pts.map(function(p){return p[0].toFixed(1)+' '+p[1].toFixed(1);}).join(' L');
  var d='M'+pts[0][0].toFixed(1)+' '+pts[0][1].toFixed(1);
  for(var i=0;i<pts.length-1;i++){
    var p0=pts[i-1]||pts[i],p1=pts[i],p2=pts[i+1],p3=pts[i+2]||pts[i+1];
    var c1x=p1[0]+(p2[0]-p0[0])/6,c1y=p1[1]+(p2[1]-p0[1])/6;
    var c2x=p2[0]-(p3[0]-p1[0])/6,c2y=p2[1]-(p3[1]-p1[1])/6;
    d+=' C'+c1x.toFixed(1)+' '+c1y.toFixed(1)+' '+c2x.toFixed(1)+' '+c2y.toFixed(1)+' '+p2[0].toFixed(1)+' '+p2[1].toFixed(1);}
  return d;}
function hhmm(ts){var dt=new Date(ts*1000);var h=dt.getHours(),m=dt.getMinutes();
  return (h<10?'0':'')+h+':'+(m<10?'0':'')+m;}
function drawHR(){
  var svg=document.getElementById('hrSvg');if(!svg)return;
  var data=HRDATA,empty=document.getElementById('hrEmpty'),meta=document.getElementById('hrMeta');
  if(!data||data.length<2){if(empty)empty.style.display='';return;}
  if(empty)empty.style.display='none';
  var L=72,R=18,T=14,B=28,W=920,H=320,pw=W-L-R,ph=H-T-B;
  var tMin=data[0].t,tMax=data[data.length-1].t;if(tMax<=tMin)tMax=tMin+1;
  var maxHr=0,i;for(i=0;i<data.length;i++){if(data[i].hr>maxHr)maxHr=data[i].hr;}
  var yMax=niceMax(maxHr*1.12);if(!(yMax>0))yMax=1;
  function X(t){return L+(t-tMin)/(tMax-tMin)*pw;}
  function Y(v){return T+ph-(Math.max(0,Math.min(v,yMax))/yMax)*ph;}
  var grid='',axis='',g,yy,val;
  for(g=0;g<=4;g++){yy=T+ph-(g/4)*ph;val=yMax*g/4;
    grid+='<line x1="'+L+'" y1="'+yy.toFixed(1)+'" x2="'+(W-R)+'" y2="'+yy.toFixed(1)+'"/>';
    axis+='<text x="'+(L-8)+'" y="'+(yy+4).toFixed(1)+'" text-anchor="end">'+hsuf(val)+'</text>';}
  var ticks=5,k,tt,xx;for(k=0;k<=ticks;k++){tt=tMin+(tMax-tMin)*k/ticks;xx=X(tt);
    axis+='<text x="'+xx.toFixed(1)+'" y="'+(H-9)+'" text-anchor="middle">'+hhmm(tt)+'</text>';}
  document.getElementById('hrGrid').innerHTML=grid;
  document.getElementById('hrAxis').innerHTML=axis;
  var pts=data.map(function(p){return [X(p.t),Y(p.hr)];});
  var dline=smoothPath(pts);
  document.getElementById('hrLine').setAttribute('d',dline);
  var base=(T+ph).toFixed(1);
  document.getElementById('hrArea').setAttribute('d',dline+' L'+pts[pts.length-1][0].toFixed(1)+' '+base+' L'+pts[0][0].toFixed(1)+' '+base+' Z');
  var G=6,means=[],j,m,sum,c,lt;
  for(j=0;j<data.length;j+=G){sum=0;c=0;lt=0;
    for(m=j;m<j+G&&m<data.length;m++){sum+=data[m].hr;c++;lt=data[m].t;}
    if(c)means.push([X(lt),Y(sum/c)]);}
  document.getElementById('hrMean').setAttribute('d',means.length>1?smoothPath(means):'');
  svg._data=data;svg._X=X;svg._Y=Y;
  var spanH=Math.round((tMax-tMin)/3600);
  if(meta)meta.textContent=(spanH>0?('spanning '+spanH+'h'):'');
}
function hrHover(ev){
  var svg=document.getElementById('hrSvg');if(!svg||!svg._data)return;
  var rect=svg.getBoundingClientRect();if(!rect.width)return;
  var vx=(ev.clientX-rect.left)/rect.width*920;
  var data=svg._data,best=0,bd=1e9,i,dx;
  for(i=0;i<data.length;i++){dx=Math.abs(svg._X(data[i].t)-vx);if(dx<bd){bd=dx;best=i;}}
  var p=data[best],px=svg._X(p.t),py=svg._Y(p.hr);
  var hov=document.getElementById('hrHover');hov.style.display='';
  var cr=document.getElementById('hrCross');cr.setAttribute('x1',px.toFixed(1));cr.setAttribute('x2',px.toFixed(1));
  var dot=document.getElementById('hrDot');dot.setAttribute('cx',px.toFixed(1));dot.setAttribute('cy',py.toFixed(1));
  var ttb='';if(NET.diff&&p.hr>0){ttb=cDur(NET.diff*Math.pow(2,32)/p.hr);}
  var tip=document.getElementById('hrTip');
  tip.innerHTML='<div class="t">'+hhmm(p.t)+'</div><div class="hv">'+hsuf(p.hr)+'</div><div class="t">'+(p.w||0)+' workers'+(ttb&&ttb!=='—'?' &middot; '+ttb+' to a block':'')+'</div>';
  tip.style.display='';
  var wrap=svg.parentNode,wr=wrap.getBoundingClientRect();
  var lx=ev.clientX-wr.left+14,ty=ev.clientY-wr.top+12;
  if(lx+150>wr.width)lx=ev.clientX-wr.left-150;if(lx<0)lx=2;
  tip.style.left=lx.toFixed(0)+'px';tip.style.top=ty.toFixed(0)+'px';
}
function hrLeave(){var h=document.getElementById('hrHover');if(h)h.style.display='none';
  var t=document.getElementById('hrTip');if(t)t.style.display='none';}
async function refresh(){
  try{
    var r=await fetch('/api/public',{cache:'no-store'});
    if(!r.ok)throw 0;
    var d=await r.json();
    if(d.network){NET.diff=d.network.difficulty;NET.hashrate=d.network.hashrate;NET.subsidy=d.network.subsidy;calc();}
    if(d.history){HRDATA=d.history;drawHR();}
    document.getElementById('s_hr1m').textContent=fmt(d.hashrate['1m']);
    document.getElementById('s_hr5m').textContent=fmt(d.hashrate['5m']);
    document.getElementById('s_hr1h').textContent=fmt(d.hashrate['1h']);
    document.getElementById('s_hr1d').textContent=fmt(d.hashrate['1d']);
    var _nh=d.network&&d.network.hashrate;
    document.getElementById('s_nethash').textContent=_nh?(_nh/1e18).toFixed(2)+' EH/s':'—';
    document.getElementById('s_blocks').textContent=d.blocks?d.blocks.length:0;
    document.getElementById('s_workers').textContent=fmt(d.workers);
    document.getElementById('s_best').textContent=grp(d.bestshare);
    var _bs=document.getElementById('s_best_sub');
    if(_bs){var _bd=(d.network&&d.network.difficulty&&d.bestshare)?Number(d.bestshare)/d.network.difficulty:0;
      _bs.textContent=_bd>0?('best ever: 1 in '+cWords(1/_bd)+' of a block'):'';}
    document.getElementById('hr1m').textContent=fmt(d.hashrate['1m']);
    document.getElementById('workers').textContent=fmt(d.workers);
    document.getElementById('updated').textContent='updated '+(d.generated_at||'');
    var dot=document.getElementById('livedot'),w=document.getElementById('liveword');
    if(d.online){dot.className='dot';w.textContent='LIVE';}
    else{dot.className='dot off';w.textContent='OFFLINE';}
  }catch(e){
    var dot=document.getElementById('livedot'),w=document.getElementById('liveword');
    if(dot){dot.className='dot off';}if(w){w.textContent='OFFLINE';}
  }
}
setInterval(refresh,15000);refresh();
var _ch=document.getElementById('calcHr');
if(_ch){_ch.addEventListener('input',calc);document.getElementById('calcUnit').addEventListener('change',calc);}
var _hs=document.getElementById('hrSvg');
if(_hs){_hs.addEventListener('pointermove',hrHover);_hs.addEventListener('pointerleave',hrLeave);
  _hs.addEventListener('touchmove',function(e){if(e.touches&&e.touches[0])hrHover(e.touches[0]);},{passive:true});}
window.addEventListener('resize',drawHR);
</script>
</body></html>""" % {
        "css": PAGE_CSS,
        "tagline": html.escape(POOL_TAGLINE),
        "pitch": html.escape(POOL_PITCH),
        "dot_cls": dot_cls,
        "live_word": live_word,
        "hr1m": html.escape(hr1m),
        "hr5m": html.escape(str(v["hashrate"]["5m"] or "—")),
        "hr1h": html.escape(str(v["hashrate"].get("1h") or "—")),
        "hr1d": html.escape(str(v["hashrate"]["1d"] or "—")),
        "nethash": html.escape(fmt_hashrate(v["network"]["hashrate"]) if (v.get("network") or {}).get("hashrate") else "—"),
        "blocksfound": str(len(v.get("blocks") or [])),
        "workers": html.escape(str(workers)),
        "best": fmt_int(v["bestshare"]),
        "updated": html.escape(str(v["generated_at"] or "—")),
        "connect": _connect_card(),
        "fee": fee,
        "rest": 100 - fee,
        "blocks": blocks_html,
        "langtoggle": _lang_toggle(lang),
        "desc": hi["desc"], "canon": hi["canon"], "hreflang": hi["hreflang"],
        "oglocale": hi["oglocale"], "oglocale_alt": hi["oglocale_alt"],
        "jsonld": _json_ld() if lang == "en" else "",
    }
    if lang != "en":
        page = _translate(page, lang)
    return page


def _svg_sparkline(hist, w=900, h=140):
    """Static inline SVG hashrate sparkline (no JS) for the SSR /users page.
    hist: list of {t,hr}. Returns '' if too few points to draw."""
    pts = [(int(p["t"]), float(p["hr"])) for p in hist if p.get("hr")]
    if len(pts) < 2:
        return ""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, ymax = min(xs), max(xs), max(ys)
    if x1 == x0 or ymax <= 0:
        return ""
    L, R, T, B = 8, 8, 18, 8
    iw, ih = w - L - R, h - T - B

    def sx(x):
        return L + (x - x0) / (x1 - x0) * iw

    def sy(y):
        return T + (1 - y / (ymax * 1.08)) * ih

    line = " ".join("%s%.1f,%.1f" % ("M" if i == 0 else "L", sx(x), sy(y))
                    for i, (x, y) in enumerate(pts))
    area = ("M%.1f,%.1f " % (sx(pts[0][0]), T + ih)
            + " ".join("L%.1f,%.1f" % (sx(x), sy(y)) for x, y in pts)
            + " L%.1f,%.1f Z" % (sx(pts[-1][0]), T + ih))
    return (
        '<svg viewBox="0 0 %d %d" preserveAspectRatio="none" role="img" '
        'aria-label="Your hashrate over the last 24 hours" '
        'style="width:100%%;height:140px;display:block">'
        '<defs><linearGradient id="spk" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#f7931a" stop-opacity="0.30"/>'
        '<stop offset="1" stop-color="#f7931a" stop-opacity="0"/></linearGradient></defs>'
        '<path d="%s" fill="url(#spk)"/>'
        '<path d="%s" fill="none" stroke="#f7931a" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
        '<text x="%d" y="13" fill="#7a8699" font-size="11" '
        'font-family="monospace">peak %s</text>'
        '</svg>') % (w, h, area, line, L, html.escape(fmt_hashrate(ymax)))


def render_user(address, lang="en"):
    safe_addr = html.escape(address)
    valid = bool(BTC_ADDR_RE.match(address))
    if not valid:
        body = ("<div class='card'><h2>Invalid address</h2>"
                "<p class='muted'>“%s” doesn't look like a Bitcoin address. "
                "Use the address you mine to.</p></div>" % safe_addr)
        return _user_shell(safe_addr, body, lang)

    v = build_address_view(address)
    if not v["found"]:
        body = ("<div class='card'><h2>No miner found</h2>"
                "<p class='muted'>No miner found for <code>%s</code> yet. "
                "Point a rig at the pool with this address as the username and "
                "your stats will appear here.</p></div>" % safe_addr)
        return _user_shell(safe_addr, body, lang)

    t = v["totals"]
    o = v.get("odds", {})
    rows = []
    for w in v["workers"]:
        dot = "<span class='dot'></span>" if w["online"] else "<span class='dot off'></span>"
        rows.append(
            "<tr><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td class='muted'>%s</td></tr>" % (
                dot, html.escape(w["worker"]),
                html.escape(w["hashrate1m"]), html.escape(w["hashrate5m"]),
                html.escape(w["hashrate1h"]), html.escape(w["hashrate1d"]),
                "{:,}".format(w["shares"]), "{:,}".format(w["bestever"]),
                fmt_unix(w["lastshare"])))
    spark = _svg_sparkline(addr_history(address))
    if spark:
        chart_card = ('<div class="card"><h2>Your hashrate · last 24h</h2>'
                      '<div class="hrwrap">%s</div>'
                      '<p class="muted" style="margin-top:8px">10-minute averages, your '
                      'workers combined. Updates as you mine.</p></div>') % spark
    else:
        chart_card = ('<div class="card"><h2>Your hashrate · last 24h</h2>'
                      '<p class="muted">History is building — check back after a bit '
                      'of mining.</p></div>')
    body = """
<div class="card">
  <h2>Totals</h2>
  <div class="grid">
    <div class="stat"><div class="k">Hashrate · 1m</div><div class="v">%(h1m)s</div></div>
    <div class="stat"><div class="k">Hashrate · 5m</div><div class="v">%(h5m)s</div></div>
    <div class="stat"><div class="k">Hashrate · 1h</div><div class="v">%(h1h)s</div></div>
    <div class="stat"><div class="k">Hashrate · 1d</div><div class="v">%(h1d)s</div></div>
    <div class="stat"><div class="k">Workers</div><div class="v">%(wc)s</div></div>
    <div class="stat"><div class="k">Shares</div><div class="v">%(sh)s</div></div>
    <div class="stat"><div class="k">Best Ever</div><div class="v">%(be)s</div></div>
    <div class="stat"><div class="k">Last Share</div><div class="v" style="font-size:14px">%(ls)s</div></div>
  </div>
</div>
<div class="card">
  <h2>Your solo odds</h2>
  <div class="grid">
    <div class="stat"><div class="k">Expected time to your block</div><div class="v">%(o_eta)s</div></div>
    <div class="stat"><div class="k">Your closest brush</div><div class="v">%(o_closest)s</div></div>
    <div class="stat"><div class="k">Your share of the network</div><div class="v">%(o_share)s</div></div>
    <div class="stat"><div class="k">Expected yield</div><div class="v">%(o_yield)s</div></div>
    <div class="stat"><div class="k">Round progress · luck</div><div class="v">%(o_luck)s</div></div>
    <div class="stat"><div class="k">Network hashrate</div><div class="v">%(o_nethash)s</div></div>
  </div>
  <p class="muted" style="margin-top:10px">True solo — you mine for the whole block reward. These odds are for <b>your</b> hashrate alone; it's a lottery, so a block can land anytime or take far longer than expected.</p>
</div>
%(chart)s
<div class="card">
  <h2>Workers</h2>
  <div class="tblwrap">
  <table><thead><tr><th>worker</th><th>1m</th><th>5m</th><th>1h</th><th>1d</th>
  <th>shares</th><th>best ever</th><th>last share</th></tr></thead>
  <tbody>%(rows)s</tbody></table>
  </div>
</div>""" % {
        "h1m": html.escape(t["hashrate1m"]), "h5m": html.escape(t["hashrate5m"]),
        "h1h": html.escape(t["hashrate1h"]), "h1d": html.escape(t["hashrate1d"]),
        "wc": t["worker_count"], "sh": "{:,}".format(t["shares"]),
        "be": "{:,}".format(t["bestever"]), "ls": fmt_unix(t["lastshare"]),
        "o_eta": html.escape(o.get("eta", "--")), "o_closest": html.escape(o.get("closest", "--")),
        "o_share": html.escape(o.get("share", "--")), "o_yield": html.escape(o.get("yield", "--")),
        "o_luck": html.escape(o.get("luck", "--")), "o_nethash": html.escape(o.get("nethash", "--")),
        "rows": "".join(rows), "chart": chart_card,
    }
    return _user_shell(safe_addr, body, lang)


def _user_shell(safe_addr, body, lang="en"):
    back = "/" if lang == "en" else ("/?lang=%s" % lang)
    page = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>%(addr)s — SoloLuck</title><link rel="icon" type="image/svg+xml" href="/favicon.svg"><style>%(css)s</style></head><body>
<div class="wrap">
<header><h1 class="brand"><span class="b">Solo</span>Luck</h1>
<p class="tagline">Asia's community solo Bitcoin pool</p>
<p class="pitch mono" style="word-break:break-all"><a href="https://mempool.space/address/%(addr)s" target="_blank" rel="noopener">%(addr)s</a></p>
<p class="muted"><a href="%(back)s">← back to pool</a></p></header>
%(body)s
<footer>SoloLuck · Asia's community solo Bitcoin pool · non-custodial · 2%% fee</footer>
</div></body></html>""" % {"addr": safe_addr, "css": PAGE_CSS, "body": body, "back": back}
    if lang != "en":
        page = _translate(page, lang)
    return page


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "SoloLuck/1.0"
    sys_version = ""  # don't leak the Python/stdlib version in the Server header

    def version_string(self):
        return self.server_version

    def log_message(self, *a):  # quiet
        pass

    # CSP is safe to lock down hard: this page loads ZERO third-party assets and
    # only ever fetches its own /api/public. Inline <style>/<script> + inline
    # event handlers need 'unsafe-inline'. Blocks clickjacking + asset injection
    # of a payout-critical stratum address.
    _CSP = ("default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'")

    def _send(self, code, body, ctype="text/html; charset=utf-8", cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Strict-Transport-Security",
                         "max-age=31536000; includeSubDomains")
        self.send_header("Content-Security-Policy", self._CSP)
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def do_HEAD(self):
        # stdlib has no HEAD handler -> uptime monitors and link-preview
        # unfurlers were getting 501. Reuse the GET router, suppress the body.
        self._head_only = True
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/health":
                self._send(200, json.dumps({"ok": True}),
                           "application/json; charset=utf-8")
            elif path == "/api/public":
                self._send(200, json.dumps(build_public_view()),
                           "application/json; charset=utf-8")
            elif path == "/" or path == "":
                import urllib.parse as _up
                q = _up.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                lang = (q.get("lang", [""])[0] or "").lower()
                if lang not in SUPPORTED_LANGS:
                    al = (self.headers.get("Accept-Language", "") or "").lower()[:2]
                    lang = al if al in SUPPORTED_LANGS else "en"
                self._send(200, render_landing(lang))
            elif path.startswith("/users/"):
                import urllib.parse as _up
                addr = _up.unquote(path[len("/users/"):]).strip().strip("/")
                q = _up.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                lang = (q.get("lang", [""])[0] or "").lower()
                if lang not in SUPPORTED_LANGS:
                    al = (self.headers.get("Accept-Language", "") or "").lower()[:2]
                    lang = al if al in SUPPORTED_LANGS else "en"
                self._send(200, render_user(addr, lang))
            elif path == "/robots.txt":
                self._send(200, "User-agent: *\nAllow: /\nSitemap: https://sololuck.io/sitemap.xml\n",
                           "text/plain; charset=utf-8")
            elif path == "/sitemap.xml":
                def _su(L):
                    return "https://sololuck.io/" if L == "en" else ("https://sololuck.io/?lang=%s" % L)
                _alts = "".join('<xhtml:link rel="alternate" hreflang="%s" href="%s"/>'
                                % (HREFLANG.get(M, M), _su(M)) for M in SUPPORTED_LANGS)
                _alts += '<xhtml:link rel="alternate" hreflang="x-default" href="https://sololuck.io/"/>'
                _urls = "".join(
                    '<url><loc>%s</loc>%s<changefreq>daily</changefreq><priority>%s</priority></url>'
                    % (_su(L), _alts, "1.0" if L == "en" else "0.8") for L in SUPPORTED_LANGS)
                self._send(200,
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                    'xmlns:xhtml="http://www.w3.org/1999/xhtml">' + _urls + '</urlset>',
                    "application/xml; charset=utf-8")
            elif path == "/teaser":
                try:
                    with open("/opt/coregrid-pool-public/teaser.html","rb") as _f:
                        self._send(200, _f.read())
                except Exception:
                    self._send(404, b"")
            elif path == "/teaser.mp4":
                try:
                    with open("/opt/coregrid-pool-public/sololuck_teaser.mp4","rb") as _f:
                        self._send(200, _f.read(), "video/mp4",
                                   cache="public, max-age=86400")
                except Exception:
                    self._send(404, b"")
            elif path == "/favicon.svg":
                self._send(200,
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                    '<rect width="64" height="64" rx="14" fill="#0b0e14"/>'
                    '<path d="M36 6 L16 36 H30 L28 58 L48 26 H34 Z" fill="#f7931a"/></svg>',
                    "image/svg+xml", cache="public, max-age=86400")
            elif path == "/og.png":
                try:
                    with open("/opt/coregrid-pool-public/og.png", "rb") as _f:
                        self._send(200, _f.read(), "image/png",
                                   cache="public, max-age=86400")
                except Exception:
                    self._send(404, b"")
            elif path == "/favicon.ico":
                self._send(204, b"")
            else:
                self._send(404, "<h1>404</h1><p><a href='/'>home</a></p>")
        except Exception:
            # Never leak a traceback to the public.
            self._send(500, "<h1>Temporarily unavailable</h1>")


def main():
    threading.Thread(target=_pool_history_sampler, daemon=True).start()
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print("SoloLuck public pool page on http://%s:%d" % (LISTEN_HOST, LISTEN_PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
