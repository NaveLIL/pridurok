"""Сборка летописи чата на основе структурного лога диалогов."""
from __future__ import annotations

import json
import re
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import user_data
from dialog_logger import LOG_DIR, analysis_log

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-я0-9_\-]{3,}")
_STOPWORDS = {
    "это", "вот", "так", "как", "что", "где", "когда", "почему", "зачем", "тут", "там",
    "или", "для", "надо", "нужно", "просто", "только", "если", "чтобы", "меня", "тебя",
    "него", "нее", "них", "вам", "нам", "она", "они", "кто", "куда", "откуда", "после",
    "перед", "тоже", "еще", "ещё", "типа", "блин", "короче", "ладно", "ок", "ага", "да",
    "нет", "чем", "твой", "мои", "мой", "твоя", "моё", "ваш", "ваша", "наш", "наша",
    "думаешь", "обычно", "делаешь", "делаете", "серьезно", "вообще", "чего", "чё", "будешь",
    "будете", "прям", "через", "до", "тема", "теме", "темы", "про", "со", "на", "по",
    "мне", "себе", "себя", "его", "ее", "её", "их", "ими", "ней", "нем", "этом", "этот",
    "эта", "эти", "тот", "та", "то", "те", "все", "всё", "всех", "всеми", "всему", "всей", "вся",
    "свои", "свой", "своя", "своё", "раз", "один", "два", "три", "кому", "чему", "кем", "кого",
    "давай", "быть", "был", "была", "было", "были", "есть", "будет", "будут", "может", "могут",
    "хочу", "хочет", "хотим", "хотите", "хотят", "можно", "нельзя", "буду", "будешь", "будем",
    "знаю", "знает", "знаем", "знаете", "знают", "думаю", "думает", "говорит", "сказал", "сказала",
    "сказали", "делать", "делает", "делают", "какой", "какая", "какое", "какие", "такой", "такая",
    "такое", "такие", "который", "которая", "которое", "которые", "уже", "завтра", "сегодня",
    "вчера", "сейчас", "теперь", "даже", "вдруг", "снова", "опять", "без", "под", "над", "пред",
    "при", "около", "между", "сквозь", "вдоль", "вместо", "кроме", "насчет", "насчёт", "хотя",
    "the", "and", "you", "not", "for", "this", "that", "with", "have", "are", "was", "were", "but"
}

STATE_FILE = LOG_DIR / "chronicle_state.json"


@dataclass(frozen=True)
class ChronicleStats:
    days: int
    total_messages: int
    unique_users: int
    channel_label: str
    top_users: list[tuple[str, int]]
    top_topics: list[tuple[str, int]]
    notes_count: int
    note_users: int
    summary_line: str


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text) if token.lower() not in _STOPWORDS]


def _load_records(days: int, channel_id: int | None = None, max_lines: int = 6000) -> list[dict[str, Any]]:
    if not analysis_log.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records: deque[dict[str, Any]] = deque(maxlen=max_lines)

    try:
        with analysis_log.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = _parse_timestamp(record.get("ts"))
                if ts is None or ts < cutoff:
                    continue
                if channel_id is not None and record.get("channel_id") != channel_id:
                    continue
                records.append(record)
    except OSError:
        return []

    return list(records)


def _top_users(records: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts: Counter[tuple[int | None, str]] = Counter()
    nick_map: dict[int, str] = {}

    for record in records:
        user_id = record.get("user_id")
        user_label = str(record.get("user") or "unknown")
        if isinstance(user_id, int):
            counts[(user_id, user_label)] += 1
            nick = user_data.get_nick(user_id)
            if nick:
                nick_map[user_id] = nick
        else:
            counts[(None, user_label)] += 1

    ranked: list[tuple[str, int]] = []
    for (user_id, label), count in counts.most_common(5):
        display = nick_map.get(user_id, label) if user_id is not None else label
        ranked.append((display, count))
    return ranked


def _top_topics(records: list[dict[str, Any]]) -> list[tuple[str, int]]:
    freq: Counter[str] = Counter()
    for record in records:
        prompt = str(record.get("prompt") or "")
        reply = str(record.get("reply") or "")
        for token in _tokenize(prompt):
            freq[token] += 2
        for token in _tokenize(reply):
            freq[token] += 1

    ranked = [(token, count) for token, count in freq.most_common(12) if count >= 3]
    return ranked[:8]


def _summary_line(top_topics: list[tuple[str, int]], top_users: list[tuple[str, int]], total_messages: int) -> str:
    if not top_topics:
        return "Неделя прошла тихо: чат больше подкидывал личные факты и редкие вопросы, чем срачи."

    topics = [topic for topic, _ in top_topics[:4]]
    topic_blob = ", ".join(topics)
    if any(topic in {"меркава", "war", "thunder", "тундра", "броня", "рб"} for topic in topics):
        return f"Главный движ недели — War Thunder и вечный спор про броню. Чат снова крутился вокруг {topic_blob}."
    if any(topic in {"пиво", "гараж", "жигули", "работ", "дом"} for topic in topics):
        return f"Чат жил гаражом, бытовухой и подколами. Больше всего крутились {topic_blob}."
    if total_messages < 20:
        return f"Тихая неделя: людей немного, но разговоры всё равно успели крутиться вокруг {topic_blob}."
    leader = top_users[0][0] if top_users else "чат"
    return f"Неделя собралась вокруг {topic_blob}, а самый заметный голос был у {leader}."


def build_channel_chronicle(channel_label: str, days: int = 7, channel_id: int | None = None) -> str:
    records = _load_records(days=days, channel_id=channel_id)
    if not records:
        return (
            f"**Летопись {channel_label} за {days} дн.**\n"
            "Пока тишина: за этот период в архиве нет нормальных сообщений."
        )

    top_users = _top_users(records)
    top_topics = _top_topics(records)

    notes_count = 0
    note_users = 0
    for data in user_data.all_users().values():
        notes = data.get("notes", []) if isinstance(data, dict) else []
        if notes:
            note_users += 1
            notes_count += len(notes)

    stats = ChronicleStats(
        days=days,
        total_messages=len(records),
        unique_users=len({str(r.get("user_id")) if r.get("user_id") is not None else str(r.get("user")) for r in records}),
        channel_label=channel_label,
        top_users=top_users,
        top_topics=top_topics,
        notes_count=notes_count,
        note_users=note_users,
        summary_line=_summary_line(top_topics, top_users, len(records)),
    )

    lines = [
        f"**Летопись {stats.channel_label} за {stats.days} дн.**",
        f"Сообщений: {stats.total_messages} | Людей: {stats.unique_users} | Запомненных фактов: {stats.notes_count} у {stats.note_users} юзеров",
    ]

    if stats.top_users:
        lines.append("Самые заметные: " + ", ".join(f"{name} × {count}" for name, count in stats.top_users[:5]))
    if stats.top_topics:
        lines.append("Главные темы: " + ", ".join(f"{topic} × {count}" for topic, count in stats.top_topics[:6]))

    lines.append(stats.summary_line)
    lines.append("Если хочешь, могу ещё делать такую летопись автоматически раз в неделю в отдельный канал.")
    return "\n".join(lines)


def load_last_post_ts() -> float | None:
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        value = data.get("last_post_ts")
        return float(value) if value is not None else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def save_last_post_ts(timestamp: float) -> None:
    payload = {"last_post_ts": float(timestamp)}
    try:
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
