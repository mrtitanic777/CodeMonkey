@echo off
rem Opens CodeMonkey in its own window (Electron). First time: cd desktop ^&^& npm install
cd /d "%~dp0desktop"
if not exist node_modules\electron\dist\electron.exe (
  echo Electron is not installed yet. Run: cd desktop ^&^& npm install
  pause
  exit /b 1
)
start "" node_modules\electron\dist\electron.exe .
