@echo off
setlocal enabledelayedexpansion
REM Build SoloLuckMiner.exe from sololuck_miner.py using PyInstaller, with the
REM cpuminer-opt engine builds BUNDLED inside the .exe (nothing to download).
REM Requires Python 3 on PATH. Produces dist\SoloLuckMiner.exe (a GUI app, no console).

echo ============================================
echo   Building SoloLuck Miner...
echo ============================================

python -m pip install --upgrade pyinstaller
if errorlevel 1 (
  echo.
  echo Could not install PyInstaller. Is Python 3 installed and on PATH?
  pause
  exit /b 1
)

REM 1) make sure the engine builds are present (download them if not)
if not exist "engine\cpuminer-sse2.exe" (
  echo.
  echo Fetching the cpuminer-opt engine builds...
  powershell -NoProfile -ExecutionPolicy Bypass -File fetch-engine.ps1
  if errorlevel 1 (
    echo.
    echo Failed to fetch the cpuminer-opt engine. Check your internet connection,
    echo or place the cpuminer-*.exe builds in an .\engine\ folder manually.
    pause
    exit /b 1
  )
)

REM 2) collect every engine build into PyInstaller --add-binary args
set ADDBIN=
for %%f in (engine\cpuminer-*.exe) do set ADDBIN=!ADDBIN! --add-binary "%%f;engine"
REM cpuminer-opt is NOT static — bundle its runtime DLLs alongside the engines
for %%f in (engine\*.dll) do set ADDBIN=!ADDBIN! --add-binary "%%f;engine"
if exist "engine\cpuminer-opt-LICENSE.txt" set ADDBIN=!ADDBIN! --add-data "engine\cpuminer-opt-LICENSE.txt;engine"
if exist "engine\cpuminer-opt-README.txt" set ADDBIN=!ADDBIN! --add-data "engine\cpuminer-opt-README.txt;engine"
if exist "engine\ENGINE-SOURCE.txt" set ADDBIN=!ADDBIN! --add-data "engine\ENGINE-SOURCE.txt;engine"

echo.
echo Bundling engine builds:
for %%f in (engine\cpuminer-*.exe) do echo    %%~nxf

python -m PyInstaller --onefile --noconsole --clean --name SoloLuckMiner !ADDBIN! sololuck_miner.py
if errorlevel 1 (
  echo.
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo ============================================
echo   Done. Your app is at:  dist\SoloLuckMiner.exe
echo.
echo   The cpuminer-opt engine is bundled inside it — just run it,
echo   paste your BTC address, and click Start Mining.
echo ============================================
pause
