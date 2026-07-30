@echo off
cd /d "%~dp0"

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw "%~dp0auto_vna.py"
    exit
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0auto_vna.py"
    exit
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -c "import tkinter" >nul 2>nul
    if %errorlevel%==0 (
        start "" py "%~dp0auto_vna.py"
        exit
    )
)

where python >nul 2>nul
if %errorlevel%==0 (
    python -c "import tkinter" >nul 2>nul
    if %errorlevel%==0 (
        start "" python "%~dp0auto_vna.py"
        exit
    )
)

echo Nie znaleziono Pythona z biblioteka tkinter.
echo Zainstaluj Python 3 ze strony python.org i zaznacz opcje Add Python to PATH.
pause
