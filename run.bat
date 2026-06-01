@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
    echo [setup] Создаю venv...
    py -3 -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)
echo [Придурок] Запуск...
python bot.py
pause
