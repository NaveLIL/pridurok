@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Придурок Bot
if not exist .venv (
    echo [setup] Создаю venv...
    py -3 -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)
:loop
echo [Придурок] Запуск...
REM Явный путь к venv python — чтобы не плодить экземпляры через py launcher
"%~dp0.venv\Scripts\python.exe" "%~dp0bot.py"
echo [Придурок] Упал или завершился. Перезапуск через 5 сек... (Ctrl+C чтобы остановить)
timeout /t 5 /nobreak >nul
goto loop

