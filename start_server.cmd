@echo off
chcp 65001 >nul
rem Запуск Secure AI Document Gateway в сетевом режиме (доступен другим компьютерам в сети).
rem Порт можно поменять здесь:
set PORT=8765

cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set PY=.venv\Scripts\python.exe
) else (
    set PY=python
)

echo Запуск сервера на порту %PORT%. Чтобы остановить, закройте это окно или нажмите Ctrl+C.
echo.
%PY% app.py ui --host 0.0.0.0 --port %PORT%
echo.
pause