#!/usr/bin/env bash
# Запуск бота Придурок на Linux/macOS с авто-рестартом
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "[setup] Создаю venv..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip -q
    .venv/bin/pip install -r requirements.txt
fi

while true; do
    echo "[Придурок] Запуск..."
    .venv/bin/python bot.py || true
    echo "[Придурок] Упал или завершился. Перезапуск через 5 сек... (Ctrl+C чтобы остановить)"
    sleep 5
done
