# SoloLuck — public solo-pool stats page

The single-file, dependency-free web front-end that powers [sololuck.io](https://sololuck.io) —
a true-solo Bitcoin mining pool. Published in the *don't-trust-verify* spirit: this is the exact
code that serves the public page, so anyone can audit what it shows and confirm it loads **nothing
from anyone but us** (no cookies, no trackers, no third-party assets, no analytics).

## What it is

- **One file, Python standard library only.** No Flask, no framework, no `pip install`. Runs on a
  stock Python 3. `ThreadingHTTPServer` bound to localhost; put nginx (or any reverse proxy) in front.
- **Reads, never writes, your pool.** It only *reads* ckpool's stats JSON and renders HTML/JSON. It
  never touches stratum, the coinbase, vardiff, or any money path.
- **Hand-rolled inline-SVG charts.** Pool-hashrate time series (live-updating) on the landing page and
  a per-address 24h sparkline on `/users/<addr>` — both drawn in-page with zero chart libraries, to
  keep the no-third-party-assets promise.
- **10 languages** via post-render phrase substitution (EN/ID/MS/JA/TH/KO/ZH/VI/FIL/HI).
- **Self-referential SEO**: per-language canonical + hreflang + `og:locale`, JSON-LD, sitemap.

## Endpoints

| Path | What |
|---|---|
| `/` | Landing: live pool stats, hashrate chart, solo-odds calculator, FAQ, connect instructions |
| `/users/<btc-address>` | Per-address page: totals, per-worker table, solo odds, 24h hashrate sparkline |
| `/api/public` | Sanitized machine-readable JSON (hashrate, workers, blocks, history) for trackers |
| `/sitemap.xml`, `/favicon.svg`, `/og.png` | static assets |

## Run it

```bash
export SOLOLUCK_STRATUM_HOST=your.pool.host       # shown in the connect instructions
export SOLOLUCK_CKPOOL_STATS_URL=http://127.0.0.1:8888/api/stats   # your ckpool stats source
python3 server.py                                 # listens on 127.0.0.1:8201
```

Then point a reverse proxy at `127.0.0.1:8201`. A minimal nginx server block and a systemd unit are
in [`deploy/`](deploy/).

### Configuration (all via env)

| Env var | Default | Meaning |
|---|---|---|
| `SOLOLUCK_STRATUM_HOST` | `127.0.0.1` | public stratum host/IP shown to miners |
| `SOLOLUCK_CKPOOL_STATS_URL` | `http://127.0.0.1:8888/api/stats` | ckpool stats JSON |
| `SOLOLUCK_ADMIN_STATE_URL` | `http://127.0.0.1:8200/api/state` | optional richer stats source (network ctx + fine history). Safe to leave unset/unreachable — the page degrades gracefully. |
| `SOLOLUCK_POOL_HISTORY` | `/opt/coregrid-pool-public/pool_history.json` | where the 24h pool-hashrate ring is persisted |
| `SOLOLUCK_ADDR_HISTORY` | `/opt/coregrid-pool-public/addr_history.json` | where the bounded per-address 24h history is persisted |

## What it deliberately does NOT expose

This is the *public* front-end. It carries no operator payout address, no admin dashboard data, no
node internals, and no secrets. The `SOLOLUCK_ADMIN_STATE_URL` source (if you use one) must already be
sanitized before this page reads it.

## License

MIT — see [LICENSE](LICENSE).
