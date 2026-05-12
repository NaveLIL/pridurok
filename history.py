"""
История диалогов: умный менеджер контекста с сохранением на диск, 
лимитом по объему текста и очисткой неактивных пользователей.
"""
import json
import os
import time
from collections import deque
from typing import List, Dict, Any

import config

# Настройки (желательно добавить их в config.py, но если их нет, используются дефолтные)
HISTORY_LIMIT = getattr(config, 'HISTORY_LIMIT', 15)           # Макс. количество сообщений
MAX_CHARS = getattr(config, 'MAX_HISTORY_CHARS', 4000)         # Макс. объем текста в истории (~1000 токенов)
TTL_SECONDS = getattr(config, 'HISTORY_TTL', 86400)            # Сколько помнить юзера (86400 сек = 24 часа)
HISTORY_FILE = getattr(config, 'HISTORY_FILE', 'history.json') # Файл для сохранения памяти

# Структура: user_id -> {"last_active": timestamp, "messages": deque}
_history: Dict[int, Dict[str, Any]] = {}


def _init_user(user_id: int) -> None:
    """Инициализирует пустую историю для пользователя, если её нет."""
    if user_id not in _history:
        _history[user_id] = {
            "last_active": time.time(),
            "messages": deque(maxlen=HISTORY_LIMIT)
        }


def _enforce_char_limit(user_id: int) -> None:
    """Удаляет старые сообщения, если суммарный текст превышает MAX_CHARS."""
    messages = _history[user_id]["messages"]
    
    # Считаем длину всех сообщений
    total_chars = sum(len(m["content"]) for m in messages)
    
    # Пока текста слишком много — выкидываем самое старое сообщение (слева)
    while total_chars > MAX_CHARS and len(messages) > 1:
        removed_msg = messages.popleft()
        total_chars -= len(removed_msg["content"])


def cleanup_inactive() -> None:
    """Удаляет из памяти пользователей, которые давно ничего не писали."""
    current_time = time.time()
    # Собираем ID тех, чье время вышло
    dead_users = [
        uid for uid, data in _history.items() 
        if current_time - data["last_active"] > TTL_SECONDS
    ]
    for uid in dead_users:
        del _history[uid]


def add(user_id: int, role: str, content: str) -> None:
    """Добавляет сообщение в историю пользователя."""
    _init_user(user_id)
    
    _history[user_id]["last_active"] = time.time()
    _history[user_id]["messages"].append({"role": role, "content": content})
    
    _enforce_char_limit(user_id)


def get(user_id: int) -> List[Dict[str, str]]:
    """Возвращает историю пользователя в виде списка словарей."""
    # При запросе истории заодно чистим старые сессии (чтобы не делать это по таймеру)
    cleanup_inactive() 
    
    if user_id not in _history:
        return []
        
    _history[user_id]["last_active"] = time.time()
    return list(_history[user_id]["messages"])


def reset(user_id: int) -> int:
    """Сбрасывает историю пользователя. Возвращает количество удаленных сообщений."""
    if user_id in _history:
        n = len(_history[user_id]["messages"])
        del _history[user_id]  # Полностью очищаем память от юзера
        return n
    return 0


# === ФУНКЦИИ ДЛЯ РАБОТЫ С ДИСКОМ ===

def save() -> None:
    """Сохраняет текущую историю в JSON файл."""
    cleanup_inactive() # Чистим мусор перед сохранением
    
    export_data = {}
    for uid, data in _history.items():
        export_data[str(uid)] = {
            "last_active": data["last_active"],
            "messages": list(data["messages"]) # deque не сериализуется напрямую
        }
        
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)


def load() -> None:
    """Загружает историю из JSON файла при старте бота."""
    if not os.path.exists(HISTORY_FILE):
        return
        
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            import_data = json.load(f)
            
        for uid_str, data in import_data.items():
            uid = int(uid_str)
            _history[uid] = {
                "last_active": data["last_active"],
                "messages": deque(data["messages"], maxlen=HISTORY_LIMIT)
            }
        # Сразу чистим тех, кто "протух", пока бот был выключен
        cleanup_inactive()
    except Exception as e:
        print(f"[История] Ошибка при загрузке: {e}")