"""Хранилище данных про пользователей: клички, факты, отношение, статистика."""
import asyncio
import json
import time
from pathlib import Path
from threading import Lock

_FILE = Path(__file__).parent / "user_data.json"
_lock = Lock()
# str(user_id) -> {"nick": str|None, "notes": list[str], "affinity": float, "interactions": int, "last_seen": float}
_data: dict[str, dict] = {}

# -------- mem0 semantic memory (ленивая инициализация) --------
_mem0 = None
_mem0_lock = Lock()


def _get_mem0():
    """Возвращает (и инициализирует при первом вызове) экземпляр mem0.Memory."""
    global _mem0
    if _mem0 is not None:
        return _mem0
    with _mem0_lock:
        if _mem0 is not None:
            return _mem0
        import config as _cfg
        from mem0 import Memory
        _mem0 = Memory.from_config({
            "llm": {
                "provider": "openai",
                "config": {
                    "model": _cfg.LMSTUDIO_MODEL,
                    "openai_base_url": _cfg.LMSTUDIO_BASE_URL,
                    "api_key": _cfg.LMSTUDIO_API_KEY,
                },
            },
            "embedder": {
                "provider": "fastembed",
                "config": {
                    "model": "BAAI/bge-small-en-v1.5",
                },
            },
            "vector_store": {
                "provider": "chroma",
                "config": {
                    "collection_name": "pridurok_memory",
                    "path": str(Path(__file__).parent / "memory_db"),
                },
            },
        })
        return _mem0


async def add_memory(user_id: int, user_msg: str, bot_reply: str) -> None:
    """Сохраняет обмен репликами в семантическую память mem0."""
    messages = [
        {"role": "user", "content": user_msg},
        {"role": "assistant", "content": bot_reply},
    ]
    await asyncio.to_thread(_get_mem0().add, messages, user_id=str(user_id))


async def clear_memory(user_id: int) -> int:
    """Удаляет все mem0-воспоминания пользователя из векторной БД."""
    try:
        result = await asyncio.to_thread(_get_mem0().get_all, user_id=str(user_id))
        items: list[dict] = result.get("results", result) if isinstance(result, dict) else (result or [])
        for item in items:
            mem_id = item.get("id")
            if mem_id:
                await asyncio.to_thread(_get_mem0().delete, mem_id)
        return len(items)
    except Exception:
        return 0


async def search_memory(user_id: int, query: str, limit: int = 5) -> list[str]:
    """Ищет релевантные воспоминания о пользователе по смыслу запроса."""
    result = await asyncio.to_thread(
        _get_mem0().search, query, user_id=str(user_id), limit=limit
    )
    # mem0 возвращает dict {"results": [...]} или список в зависимости от версии
    items: list[dict] = result.get("results", result) if isinstance(result, dict) else (result or [])
    return [r["memory"] for r in items if isinstance(r, dict) and r.get("memory")]


def _load() -> None:
    global _data
    if _FILE.exists():
        try:
            with open(_FILE, "r", encoding="utf-8") as f:
                _data = json.load(f)
        except (json.JSONDecodeError, OSError):
            _data = {}


def _save() -> None:
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(_data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _get_entry(user_id: int) -> dict:
    key = str(user_id)
    if key not in _data:
        _data[key] = {
            "nick": None,
            "notes": [],
            "affinity": 0.0,
            "interactions": 0,
            "last_seen": 0.0,
        }
    e = _data[key]
    # Миграция старых записей
    e.setdefault("affinity", 0.0)
    e.setdefault("interactions", 0)
    e.setdefault("last_seen", 0.0)
    e.setdefault("notes", [])
    return e


def get_nick(user_id: int) -> str | None:
    with _lock:
        return _get_entry(user_id).get("nick")


def set_nick(user_id: int, nick: str | None) -> None:
    with _lock:
        _get_entry(user_id)["nick"] = nick
        _save()


def get_notes(user_id: int) -> list[str]:
    with _lock:
        return list(_get_entry(user_id).get("notes", []))


def add_note(user_id: int, note: str) -> int:
    with _lock:
        notes = _get_entry(user_id).setdefault("notes", [])
        # Дедупликация по подстроке
        note_lower = note.lower().strip()
        for existing in notes:
            if note_lower in existing.lower() or existing.lower() in note_lower:
                return len(notes)
        notes.append(note.strip())
        if len(notes) > 20:
            notes.pop(0)
        _save()
        return len(notes)


def clear_notes(user_id: int) -> int:
    with _lock:
        notes = _get_entry(user_id).get("notes", [])
        n = len(notes)
        _get_entry(user_id)["notes"] = []
        _save()
        return n


def record_interaction(user_id: int, affinity_delta: float = 0.0) -> None:
    """Регистрирует факт взаимодействия + сдвигает отношение."""
    with _lock:
        e = _get_entry(user_id)
        e["interactions"] = int(e.get("interactions", 0)) + 1
        e["last_seen"] = time.time()
        if affinity_delta != 0.0:
            cur = float(e.get("affinity", 0.0))
            e["affinity"] = max(-1.0, min(1.0, cur + affinity_delta))
        _save()


def get_affinity(user_id: int) -> float:
    with _lock:
        return float(_get_entry(user_id).get("affinity", 0.0))


def get_interactions(user_id: int) -> int:
    with _lock:
        return int(_get_entry(user_id).get("interactions", 0))


def get_relationship_label(user_id: int) -> str | None:
    """Текстовое отношение для подстановки в промпт."""
    aff = get_affinity(user_id)
    inter = get_interactions(user_id)
    if inter < 5:
        return None  # рано судить
    if aff <= -0.6:
        return "заклятый враг — ненавидишь его особенно сильно"
    if aff <= -0.2:
        return "противный нытик, который тебя бесит"
    if aff >= 0.6:
        return "почти терпимый кент, к которому ты привык (но всё равно подъёбываешь)"
    if aff >= 0.2:
        return "нейтрально терпимый, можешь иногда соглашаться"
    return None


def all_users() -> dict[str, dict]:
    with _lock:
        return dict(_data)


_load()

