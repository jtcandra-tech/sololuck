@echo off
REM Build SoloLuckMiner.exe from sololuck_miner.py using PyInstaller.
REM This is a CLEAN wrapper — no engine is bundled; the app downloads the
REM cpuminer-opt engine itself on first run. Requires Python 3 on PATH.
REM Produces dist\SoloLuckMiner.exe (a small GUI app, no console).

echo ============================================
echo   Building SoloLuck Miner (clean wrapper)...
echo ============================================

python -m pip install --upgrade pyinstaller
if errorlevel 1 (
  echo.
  echo Could not install PyInstaller. Is Python 3 installed and on PATH?
  pause
  exit /b 1
)

python -m PyInstaller --onefile --noconsole --clean --name SoloLuckMiner sololuck_miner.py
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
echo   Just run it — it downloads the matching cpuminer-opt
echo   engine on first launch, then you paste your BTC address
echo   and click Start Mining.
echo ============================================
pause
