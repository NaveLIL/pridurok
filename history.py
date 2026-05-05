"""История диалогов: на каждого пользователя свой контекст. Сохраняется на диск.
Поддерживает суммаризацию — старые сообщения сжимаются в одну summary-строку.
"""
import json
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock
from typing import Deque

import config

_FILE = Path(__file__).parent / "history.json"
_lock = Lock()


def _make_deque(items=None) -> Deque[dict]:
    d: Deque[dict] = deque(maxlen=config.HISTORY_LIMIT)
    if items:
        for item in items:
            d.append(item)
    return d


# user_id -> deque[{"role": "user"/"assistant", "content": str}]
_history: dict[int, Deque[dict]] = defaultdict(_make_deque)
# user_id -> str (краткая сводка прошлых разговоров)
_summary: dict[int, str] = {}


def _load() -> None:
    if _FILE.exists():
        try:
            with open(_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # Поддержка старого формата (просто dict[uid, list]) и нового (dict с "messages"/"summary")
            for uid, data in raw.items():
                if isinstance(data, list):
                    _history[int(uid)] = _make_deque(data)
                elif isinstance(data, dict):
                    _history[int(uid)] = _make_deque(data.get("messages", []))
                    s = data.get("summary")
                    if s:
                        _summary[int(uid)] = s
        except (json.JSONDecodeError, OSError, ValueError):
            pass


def _save() -> None:
    try:
        snapshot = {}
        all_uids = set(_history.keys()) | set(_summary.keys())
        for uid in all_uids:
            entry: dict = {}
            if uid in _history and _history[uid]:
                entry["messages"] = list(_history[uid])
            if uid in _summary and _summary[uid]:
                entry["summary"] = _summary[uid]
            if entry:
                snapshot[str(uid)] = entry
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def add(user_id: int, role: str, content: str) -> None:
    with _lock:
        _history[user_id].append({"role": role, "content": content})
        _save()


def get(user_id: int) -> list[dict]:
    return list(_history[user_id])


def get_summary(user_id: int) -> str | None:
    return _summary.get(user_id) or None


def set_summary(user_id: int, summary: str) -> None:
    with _lock:
        _summary[user_id] = summary.strip()
        _save()


def needs_summarization(user_id: int) -> bool:
    """Если буфер переполнен — пора сжимать."""
    return len(_history[user_id]) >= config.HISTORY_LIMIT


def reset(user_id: int) -> int:
    with _lock:
        n = len(_history[user_id])
        _history[user_id].clear()
        _summary.pop(user_id, None)
        _save()
        return n


def trim_after_summary(user_id: int, keep_last: int = 4) -> None:
    """После создания summary оставляем только последние N сообщений."""
    with _lock:
        items = list(_history[user_id])[-keep_last:]
        _history[user_id] = _make_deque(items)
        _save()


_load()


