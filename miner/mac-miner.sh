#!/usr/bin/env bash
# SoloLuck — Mac CPU solo miner (Intel + Apple Silicon).
# Finds or builds a CPU miner from source and points it at sololuck.io, mining
# Bitcoin solo to YOUR own address. No Apple account needed (terminal, not a
# notarized app). A Mac's hashrate is tiny, so this is a low-power lottery ticket.
#
#   curl -fsSL https://sololuck.io/mac-miner.sh | bash
#   ./mac-miner.sh bc1qyouraddress
#
# Open-source, non-custodial: the reward (0% pool fee — finders keepers) on a solved
# block) is paid straight to your address on-chain. We never hold it.
set -uo pipefail

POOL_HOST="stratum.sololuck.io"
POOL_PORT="3335"            # Nano tier (difficulty 1) — tuned for CPUs
ALGO="sha256d"
WORKER="mac"
WORKDIR="$HOME/.sololuck-miner"

c_y(){ printf '\033[1;33m%s\033[0m\n' "$*"; }
c_r(){ printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
c_g(){ printf '\033[1;32m%s\033[0m\n' "$*"; }

c_y "── SoloLuck · Mac CPU solo miner ───────────────────────────────"
echo "Mines Bitcoin (CPU) solo to YOUR address via $POOL_HOST:$POOL_PORT."
echo "Solo mining is a lottery — a Mac's hashrate is tiny, so think of it as a"
echo "cheap, low-power ticket. If your Mac solves a block, the whole reward is"
echo "yours (0% pool fee, finders keepers), paid on-chain to your address. No account."
echo

[ "$(uname)" = "Darwin" ] || { c_r "This installer is for macOS. For Windows see https://sololuck.io/setup"; exit 1; }
ARCH="$(uname -m)"   # arm64 (Apple Silicon) | x86_64 (Intel)
echo "Detected: macOS on $ARCH"

# ── BTC payout address ────────────────────────────────────────────────────────
ADDR="${1:-}"
if [ -z "$ADDR" ]; then
  printf "Paste your Bitcoin payout address (bc1q…): "
  read -r ADDR < /dev/tty || true
fi
case "$ADDR" in
  bc1*|1*|3*) : ;;
  *) c_r "That doesn't look like a Bitcoin address. Aborting."; exit 1 ;;
esac

# ── locate an already-installed engine ────────────────────────────────────────
find_engine(){
  local c p
  for c in cpuminer-opt cpuminer minerd cpuminer-multi; do
    command -v "$c" >/dev/null 2>&1 && { command -v "$c"; return 0; }
  done
  for p in /opt/homebrew/bin /usr/local/bin /opt/local/bin \
           "$WORKDIR/cpuminer-opt" "$WORKDIR/cpuminer-pooler"; do
    for c in cpuminer-opt cpuminer minerd; do
      [ -x "$p/$c" ] && { echo "$p/$c"; return 0; }
    done
  done
  return 1
}
ENGINE="$(find_engine || true)"

# ── build one from source if needed ───────────────────────────────────────────
if [ -z "$ENGINE" ]; then
  c_y "No CPU miner found — I'll build one from source (this can take a few minutes)."

  if ! xcode-select -p >/dev/null 2>&1; then
    c_y "Installing Xcode Command Line Tools — a system dialog will appear; click Install."
    xcode-select --install >/dev/null 2>&1 || true
    c_r "Re-run this command once the Command Line Tools have finished installing."
    exit 1
  fi

  BREW=""
  for b in brew /opt/homebrew/bin/brew /usr/local/bin/brew; do
    command -v "$b" >/dev/null 2>&1 && { BREW="$(command -v "$b")"; break; }
    [ -x "$b" ] && { BREW="$b"; break; }
  done
  if [ -z "$BREW" ]; then
    c_y "Homebrew (the standard Mac package manager) is needed to fetch build tools."
    printf "Install Homebrew now? [y/N]: "; read -r yn < /dev/tty || true
    case "$yn" in
      y|Y) /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" ;;
      *) c_r "Can't build without it. Manual steps: https://sololuck.io/setup"; exit 1 ;;
    esac
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && BREW="$b"; done
    [ -n "$BREW" ] || { c_r "Homebrew not found after install. See https://sololuck.io/setup"; exit 1; }
  fi

  c_y "Installing build dependencies…"
  "$BREW" install autoconf automake libtool pkg-config curl jansson openssl@3 gmp >/dev/null 2>&1 || \
    "$BREW" install autoconf automake libtool pkg-config curl jansson openssl@3 gmp || true
  BP="$("$BREW" --prefix)"
  export CPPFLAGS="-I$BP/include -I$BP/opt/openssl@3/include -I$BP/opt/curl/include ${CPPFLAGS:-}"
  export LDFLAGS="-L$BP/lib -L$BP/opt/openssl@3/lib -L$BP/opt/curl/lib ${LDFLAGS:-}"
  export PKG_CONFIG_PATH="$BP/opt/openssl@3/lib/pkgconfig:$BP/opt/curl/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
  JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 2)"
  mkdir -p "$WORKDIR"; cd "$WORKDIR"

  # Primary: cpuminer-opt (actively maintained; builds on macOS incl. Apple Silicon).
  c_y "Fetching + building cpuminer-opt…"
  rm -rf cpuminer-opt
  if git clone --depth 1 https://github.com/JayDDee/cpuminer-opt.git >/dev/null 2>&1 \
     && cd cpuminer-opt && ./autogen.sh >/dev/null 2>&1 \
     && ./configure CFLAGS="-O3" >/dev/null 2>&1 \
     && make -j"$JOBS" >/dev/null 2>&1 && [ -x ./cpuminer ]; then
    ENGINE="$WORKDIR/cpuminer-opt/cpuminer"
  else
    cd "$WORKDIR"
    c_r "cpuminer-opt didn't build cleanly — falling back to pooler/cpuminer…"
    rm -rf cpuminer-pooler
    if git clone --depth 1 https://github.com/pooler/cpuminer.git cpuminer-pooler >/dev/null 2>&1 \
       && cd cpuminer-pooler && ./autogen.sh >/dev/null 2>&1 \
       && ./configure CFLAGS="-O3" >/dev/null 2>&1 \
       && make -j"$JOBS" >/dev/null 2>&1 && [ -x ./minerd ]; then
      ENGINE="$WORKDIR/cpuminer-pooler/minerd"
    else
      c_r "Automatic build failed on this Mac. Please follow the manual steps at https://sololuck.io/setup"
      exit 1
    fi
  fi
fi

[ -n "$ENGINE" ] && [ -x "$ENGINE" ] || { c_r "No runnable engine. See https://sololuck.io/setup"; exit 1; }

# leave one core free so the Mac stays usable
THREADS="$(sysctl -n hw.ncpu 2>/dev/null || echo 2)"
[ "$THREADS" -gt 1 ] 2>/dev/null && THREADS=$((THREADS-1)) || THREADS=1

c_g "Engine ready: $ENGINE"
c_g "Mining to ${ADDR}.${WORKER} on $POOL_HOST:$POOL_PORT  (Ctrl-C to stop)"
echo
exec "$ENGINE" -a "$ALGO" -o "stratum+tcp://$POOL_HOST:$POOL_PORT" -u "${ADDR}.${WORKER}" -p x -t "$THREADS"
