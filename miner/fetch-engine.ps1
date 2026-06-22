# fetch-engine.ps1 — download the cpuminer-opt Windows engine builds that
# SoloLuck Miner bundles, into .\engine\. Run automatically by build.bat when
# .\engine\ is missing; safe to run by hand to refresh the engine.
#
# cpuminer-opt is GPLv2 by Jay D Dee — https://github.com/JayDDee/cpuminer-opt
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo = "JayDDee/cpuminer-opt"
# the CPU variants we ship — fastest..safest; the app auto-picks the right one
$want = @(
  "cpuminer-avx512-sha-vaes.exe",
  "cpuminer-avx512.exe",
  "cpuminer-avx2-sha-vaes.exe",
  "cpuminer-avx2-sha.exe",
  "cpuminer-avx2.exe",
  "cpuminer-aes-sse42.exe",
  "cpuminer-sse2.exe"
)

Write-Host "Querying latest cpuminer-opt release ($repo)..."
$rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" `
       -Headers @{ "User-Agent" = "sololuck-miner-build" }
$asset = $rel.assets | Where-Object { $_.name -match "windows.*\.zip$" -or $_.name -match "win64.*\.zip$" } | Select-Object -First 1
if (-not $asset) {
  $asset = $rel.assets | Where-Object { $_.name -match "\.zip$" } | Select-Object -First 1
}
if (-not $asset) { throw "No Windows .zip asset found on the latest cpuminer-opt release." }
Write-Host ("Found {0} ({1})" -f $asset.name, $rel.tag_name)

$zip = Join-Path $env:TEMP $asset.name
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing
$tmp = Join-Path $env:TEMP "sololuck-cpuminer-extract"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $tmp -Force

New-Item -ItemType Directory -Force -Path "engine" | Out-Null
$got = 0
foreach ($w in $want) {
  $f = Get-ChildItem -Path $tmp -Recurse -Filter $w -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($f) {
    Copy-Item $f.FullName -Destination (Join-Path "engine" $w) -Force
    Write-Host ("  + {0}" -f $w)
    $got++
  }
}
if ($got -eq 0) { throw "Extracted the zip but found none of the expected cpuminer-*.exe builds." }

# cpuminer-opt is NOT statically linked — copy its runtime DLLs too, or the engines
# won't launch ("the program can't start because libcurl-4.dll is missing").
$dlls = @("libcurl-4.dll", "libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll", "zlib1.dll")
foreach ($d in $dlls) {
  $f = Get-ChildItem -Path $tmp -Recurse -Filter $d -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($f) { Copy-Item $f.FullName -Destination (Join-Path "engine" $d) -Force; Write-Host ("  + {0}" -f $d) }
}
# also any other DLLs shipped in the zip (future-proofing)
Get-ChildItem -Path $tmp -Recurse -Filter "*.dll" -ErrorAction SilentlyContinue | ForEach-Object {
  $dest = Join-Path "engine" $_.Name
  if (-not (Test-Path $dest)) { Copy-Item $_.FullName -Destination $dest -Force; Write-Host ("  + {0}" -f $_.Name) }
}

# ship cpuminer-opt's licence alongside the engine (GPLv2 compliance). The Windows
# zip usually omits it, so fall back to fetching COPYING from the source repo.
$lic = Get-ChildItem -Path $tmp -Recurse -Include "COPYING", "LICENSE*", "COPYING.txt" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($lic) {
  Copy-Item $lic.FullName -Destination "engine\cpuminer-opt-LICENSE.txt" -Force
  Write-Host "  + cpuminer-opt-LICENSE.txt (from zip)"
} else {
  try {
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/$repo/master/COPYING" `
      -OutFile "engine\cpuminer-opt-LICENSE.txt" -UseBasicParsing
    Write-Host "  + cpuminer-opt-LICENSE.txt (GPLv2, from repo)"
  } catch {
    Write-Host "  ! could not fetch COPYING; ENGINE-SOURCE.txt still points to the GPLv2 source"
  }
}
# bundle the engine's own README too (handy reference)
$rme = Get-ChildItem -Path $tmp -Recurse -Filter "README.txt" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($rme) { Copy-Item $rme.FullName -Destination "engine\cpuminer-opt-README.txt" -Force }
"cpuminer-opt $($rel.tag_name) — engine builds bundled with SoloLuck Miner.`r`nSource: https://github.com/$repo`r`nLicence: GPLv2 (see cpuminer-opt-LICENSE.txt)" |
  Out-File -Encoding utf8 "engine\ENGINE-SOURCE.txt"

Write-Host ("`nDone — {0} engine build(s) in .\engine\" -f $got)
