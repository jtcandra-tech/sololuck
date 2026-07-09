$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$work = 'C:\Users\Public\sl-build'
Set-Location $work

# ensure a working Python 3.12
$py = $null
foreach ($cand in @('C:\Program Files\Python312\python.exe',
                    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
  if (Test-Path $cand) { $py = $cand; break }
}
if (-not $py) {
  Write-Host '== installing Python 3.12.7 =='
  $inst = "$env:TEMP\py312.exe"
  Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile $inst -UseBasicParsing
  Start-Process $inst -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1 Include_tcltk=1 Include_test=0' -Wait
  $py = 'C:\Program Files\Python312\python.exe'
}
$pydir = Split-Path $py
$env:PATH = "$pydir;$pydir\Scripts;$env:PATH"
Write-Host ('python: ' + (& $py --version 2>&1))
& $py -m pip install --upgrade pip pyinstaller 2>&1 | Select-String -Pattern 'Successfully|already|error' | Select-Object -First 4
Write-Host ('pyinstaller: ' + (& $py -m PyInstaller --version 2>&1))

# clean wrapper — no engine bundled
if (Test-Path 'dist\SoloLuckMiner.exe') { Remove-Item 'dist\SoloLuckMiner.exe' -Force }
& $py -m PyInstaller --onefile --noconsole --clean --name SoloLuckMiner sololuck_miner.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$exe = Get-Item 'dist\SoloLuckMiner.exe'
Write-Host ('== BUILT: ' + $exe.FullName + ' (' + [math]::Round($exe.Length/1MB,1) + ' MB) ==')
(Get-FileHash $exe.FullName -Algorithm MD5).Hash
