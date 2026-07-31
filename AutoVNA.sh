#!/usr/bin/env bash
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    if python3 -c "import tkinter" >/dev/null 2>&1; then
        nohup python3 "$(pwd)/auto_vna.py" >/dev/null 2>&1 &
        exit 0
    fi
fi

echo "Nie znaleziono Python 3 z biblioteką tkinter."
echo "Ubuntu/Debian: sudo apt install python3 python3-tk"
read -r
