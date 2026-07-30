#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
if ! python3 -c "import serial,sys; sys.exit(0 if hasattr(serial,'Serial') else 1)" >/dev/null 2>&1; then
    python3 -m pip uninstall -y serial
    python3 -m pip install --force-reinstall pyserial
fi
python3 main.py
