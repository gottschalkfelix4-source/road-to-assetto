@echo off
cd /d "%~dp0"
python -m webapp.server --host 127.0.0.1 --port 8765
pause
