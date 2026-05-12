Краткая инструкция по деплою на VPS

1) Требования
- На машине-источнике: `rsync`, `scp`, `ssh` (WSL/git-bash на Windows рекомендован).
- На VPS: доступ по SSH под пользователем с правом выполнять `sudo systemctl restart pridurok.service`.

2) Быстрый запуск (bash)
```bash
# из корня репозитория
./scripts/deploy.sh user@vps.example.com /opt/pridurok --env-file ./.env
```

3) Windows (PowerShell, с rsync доступным в PATH)
```powershell
.\scripts\deploy.ps1 -Remote user@vps.example.com -RemotePath /opt/pridurok -EnvFile .\.env
```

4) Что делают скрипты
- Синхронизируют репозиторий (без `.venv`, `memory_db`, `logs`) в временную папку на сервере.
- Копируют локальный `.env` в `/tmp/pridurok.env` на сервере и перемещают его в `/opt/pridurok/.env`.
- Очищают возможные BOM (U+FEFF) в `*.py` на сервере.
- Перезапускают `pridurok.service` и выводят статус + последние логи.

5) Если хочешь, могу выполнить эти команды сам: пришли SSH-строку `user@host` и укажи, используем ли существующий SSH-ключ (рекомендуется). Без ваших учётных данных я не могу подключиться автоматически.
