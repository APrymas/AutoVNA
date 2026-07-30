@echo off
cd /d "%~dp0"
py -m pip install -r requirements.txt
py -c "import serial,sys; sys.exit(0 if hasattr(serial,'Serial') else 1)" >nul 2>&1
if errorlevel 1 (
    py -m pip uninstall -y serial
    py -m pip install --force-reinstall pyserial
)
py main.py
pause
