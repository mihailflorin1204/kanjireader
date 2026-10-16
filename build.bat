@echo off
cd /d "%~dp0"
py -m PyInstaller --noconfirm --clean --onefile --windowed --name KanjiSpeaker --collect-all pykakasi --collect-submodules pyttsx3 main.py
echo.
echo Done: dist\KanjiSpeaker.exe
pause
