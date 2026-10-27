@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo No se encontro Python. Instalalo desde python.org marcando "Add to PATH".
  pause
  exit /b 1
)
python panel.py %*
