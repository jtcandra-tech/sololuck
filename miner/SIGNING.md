# Code-signing SoloLuckMiner.exe

## What signing does — and doesn't do (read this first)
- ✅ Removes the SmartScreen **"Windows protected your PC — unknown publisher"** prompt
  (instantly with an **EV** cert; over time as reputation builds with a standard **OV** cert).
- ✅ Cuts generic "unknown unsigned binary" heuristic flags.
- ❌ Does **NOT** stop antivirus from quarantining/blocking the bundled **cpuminer** engine
  (`PUA/Trojan:Win32/CoinMiner`, the `WinError 2` / `WinError 225` users hit). That's
  *coin-miner detection* — it matches the miner's code, signed or not. A signed miner is
  still detected as a miner. Users still need the in-app allow/exclude step.

So: sign for trust & polish, but keep the allow/exclude UX. To actually reduce AV blocks,
also submit each release hash to Microsoft (https://www.microsoft.com/en-us/wdsi/filesubmission,
category "false positive") — note it's per-hash and resets on every rebuild.

## Getting a certificate (you do this — needs identity/payment)
Since 2023, OV/EV code-signing keys must live on a hardware token or cloud HSM (no plain
`.pfx` from major CAs anymore). Options, cheapest-first:
- **Azure Trusted Signing** (~$10/month) — *recommended*. Cloud-signed via the Azure
  dlib + `signtool`. Individual or org identity validation. Modern, automatable.
- **EV cert on a token** (Sectigo/DigiCert, ~$300–600/yr) — instant SmartScreen reputation,
  but signing must run on the machine with the USB token.
- **OV cert** (~$200/yr) — cheaper, SmartScreen reputation builds over a few weeks.

## Signing — two pipelines (both wired up)

### A) File-based cert (PFX/P12) — Linux side, `sign-exe.sh`
For a test cert or any `.pfx` you hold. After building+retrieving the exe on agentic:
```
CODESIGN_PFX=/path/cert.pfx CODESIGN_PASS=secret ./sign-exe.sh SoloLuckMiner.exe
```
Uses `osslsigncode` with SHA-256 + an RFC-3161 timestamp (Sectigo by default; override with
`CODESIGN_TS`). Then re-host the signed `SoloLuckMiner.exe` (scp to kvm `/opt/coregrid-pool-public/`).
*Validated working* (signs + timestamps a real Sectigo TSA; only the trust chain differs by cert.)

### B) Azure Trusted Signing / token cert — Windows side (maincctv), `signtool`
The modern path. On the Windows build box, after `build.bat` produces `dist\SoloLuckMiner.exe`:
```
signtool sign /v /fd SHA256 /tr http://timestamp.acs.microsoft.com /td SHA256 ^
  /dlib "<AzureCodeSigning.dll path>" /dmdf metadata.json dist\SoloLuckMiner.exe
```
`metadata.json` holds your Trusted Signing account/profile (`Endpoint`, `CodeSigningAccountName`,
`CertificateProfileName`). For an EV USB token, instead: `signtool sign /fd SHA256 /a /tr <ts> /td SHA256`.

## Notes
- Sign the **wrapper** `SoloLuckMiner.exe` — that's what the user double-clicks, so that's what
  SmartScreen judges. The bundled `cpuminer-*.exe` engines run as child processes (no SmartScreen
  prompt), and signing them won't clear their CoinMiner detection anyway.
- Always timestamp (`-ts` / `/tr`) so signatures stay valid after the cert expires.
- Tell us the cert route and we plug the values into the pipeline above.
