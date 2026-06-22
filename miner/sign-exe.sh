#!/usr/bin/env bash
# sign-exe.sh — Authenticode-sign a Windows .exe with osslsigncode (Linux-side).
# Use this when your code-signing cert is a FILE (PFX/P12). For modern token/HSM or
# Azure Trusted Signing certs, sign on Windows with signtool instead (see SIGNING.md).
#
#   CODESIGN_PFX=/path/cert.pfx CODESIGN_PASS=secret ./sign-exe.sh in.exe [out.exe]
#
# Env:
#   CODESIGN_PFX   path to the .pfx/.p12 code-signing cert            (required)
#   CODESIGN_PASS  its password                                       (required)
#   CODESIGN_TS    RFC-3161 timestamp URL  (default: Sectigo)         (optional)
#   CODESIGN_NAME  signature description    (default: "SoloLuck Miner")
set -euo pipefail
IN="${1:?usage: sign-exe.sh in.exe [out.exe]}"
OUT="${2:-$IN}"
: "${CODESIGN_PFX:?set CODESIGN_PFX to your .pfx cert}"
: "${CODESIGN_PASS:?set CODESIGN_PASS to the cert password}"
TS="${CODESIGN_TS:-http://timestamp.sectigo.com}"
NAME="${CODESIGN_NAME:-SoloLuck Miner}"

tmp="$(mktemp -u --suffix=.exe)"
echo "Signing $IN  (cert: $CODESIGN_PFX, ts: $TS)"
osslsigncode sign \
  -pkcs12 "$CODESIGN_PFX" -pass "$CODESIGN_PASS" \
  -h sha256 -n "$NAME" -i "https://sololuck.io" \
  -ts "$TS" \
  -in "$IN" -out "$tmp"
mv -f "$tmp" "$OUT"
echo "--- verify ---"
osslsigncode verify "$OUT" 2>&1 | grep -Ei 'Signature|Timestamp|Subject|Number of|verification' || true
echo "Signed -> $OUT"
