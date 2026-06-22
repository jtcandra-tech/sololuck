@echo off
REM Build SoloLuckMiner.exe from sololuck_miner.py using PyInstaller.
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
echo   Put a cpuminer-opt build (e.g. cpuminer-opt.exe or
echo   cpuminer-avx2.exe) in the SAME folder as the .exe,
echo   then run SoloLuckMiner.exe.
echo ============================================
pause
