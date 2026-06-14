"""Discord-бот «Придурок» — мост между Discord и LLM через OpenRouter API."""
import asyncio
import collections
import datetime
import email.utils
import json
import random
import re
import time
import traceback
from pathlib import Path

import discord
from discord import app_commands

import chronicle
import config
import history
import llm
import safety
import user_data
import search_engine
from dialog_logger import log_dialog, system as syslog


# -------- Discord setup --------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# -------- Очередь запросов: один воркер, чтобы не насиловать GPU --------
request_queue: "asyncio.Queue[asyncio.Task]" = asyncio.Queue()
spar_targets: dict[int, float] = {}
_user_last_request: dict[int, float] = {}   # user_id -> timestamp
_channel_last_message: dict[int, float] = {}  # channel_id -> timestamp
_user_request_window: dict[int, collections.deque[float]] = {}
_channel_activity_window: dict[int, collections.deque[float]] = {}
_channel_recent_topics: dict[int, collections.deque[tuple[float, str]]] = {}
_channel_last_auto_reply: dict[int, float] = {}

_STOPWORDS = {
    "это", "вот", "так", "как", "что", "где", "когда", "почему", "зачем", "тут", "там",
    "или", "для", "надо", "нужно", "просто", "только", "если", "чтобы", "меня", "тебя",
    "него", "нее", "них", "вам", "нам", "она", "они", "кто", "куда", "откуда", "после",
    "перед", "тоже", "еще", "ещё", "типа", "блин", "короче", "ладно", "ок", "ага", "да",
    "нет", "чем", "твой", "мои", "мой", "твоя", "моё", "ваш", "ваша", "наш", "наша",
    "думаешь", "обычно", "делаешь", "делаете", "серьезно", "вообще", "чего", "чето", "чё",
    "будешь", "будете", "прям", "просто", "через", "после", "до", "тема", "теме", "темы",
    "про",
}


async def _queue_worker():
    while True:
        job = await request_queue.get()
        try:
            await job
        except Exception:
            syslog.error("Ошибка в job:\n%s", traceback.format_exc())
        finally:
            request_queue.task_done()


def _task_done_callback(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        syslog.error(
            "Background task %s failed:\n%s",
            task.get_name() if hasattr(task, "get_name") else str(task),
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )


def _spawn_task(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_task_done_callback)
    return task


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
    syslog.error("Unhandled exception in event loop: %s", context)


# -------- Утилиты --------
def _split_for_discord(text: str, limit: int = config.DISCORD_MSG_LIMIT) -> list[str]:
    """Разбивает длинный текст на куски ≤ limit, стараясь резать по переводам строк."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut == -1 or cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def _user_allowed(member: discord.abc.User) -> bool:
    if not config.ALLOWED_ROLES:
        return True
    if isinstance(member, discord.Member):
        return any(r.id in config.ALLOWED_ROLES for r in member.roles)
    return True  # DM — всегда можно


def _is_admin(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    if config.ADMIN_ROLES and any(r.id in config.ADMIN_ROLES for r in member.roles):
        return True
    return False


def _should_respond(message: discord.Message) -> bool:
    if message.author.bot:
        return False
    if not message.content.strip():
        return False
    # DM
    if message.guild is None:
        return True
    # Активный канал — отвечаем на всё
    if message.channel.id in config.ACTIVE_CHANNELS:
        return True
    # Упоминание бота
    if client.user and client.user.mentioned_in(message) and not message.mention_everyone:
        return True
    return False


def _clean_prompt(message: discord.Message) -> str:
    text = message.content
    if client.user:
        text = text.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "")
    return text.strip()


def _friendly_llm_error(e: Exception) -> str:
    text = str(e)
    lower = text.lower()
    status_code = getattr(e, "status_code", None)
    if status_code == 429 or "rate limit" in lower or "free-models-per-day" in lower or "'code': 429" in lower:
        return "⚠️ Лимит запросов на сегодня выбит. Попробуй позже."
    return "⚠️ Ща не отвечу, что-то заклинило. Попробуй ещё раз чуть позже."


def _tokenize(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-zА-Яа-я0-9]{3,}", text)
        if token.lower() not in _STOPWORDS
    }


def _record_user_request(user_id: int, now: float) -> int:
    bucket = _user_request_window.setdefault(user_id, collections.deque())
    bucket.append(now)
    hour_ago = now - 3600
    while bucket and bucket[0] < hour_ago:
        bucket.popleft()
    return len(bucket)


def _record_channel_activity(channel_id: int, now: float) -> int:
    bucket = _channel_activity_window.setdefault(channel_id, collections.deque())
    bucket.append(now)
    minute_ago = now - 60
    while bucket and bucket[0] < minute_ago:
        bucket.popleft()
    return len(bucket)


def _current_channel_activity(channel_id: int, now: float) -> int:
    bucket = _channel_activity_window.setdefault(channel_id, collections.deque())
    minute_ago = now - 60
    while bucket and bucket[0] < minute_ago:
        bucket.popleft()
    return len(bucket)


def _record_channel_topic(channel_id: int, prompt: str, now: float) -> None:
    prompt = prompt.strip()
    if not prompt:
        return
    bucket = _channel_recent_topics.setdefault(channel_id, collections.deque())
    bucket.append((now, prompt[:240]))
    while len(bucket) > 120:
        bucket.popleft()
    cutoff = now - 3600
    while bucket and bucket[0][0] < cutoff:
        bucket.popleft()


def _collect_channel_topics(channel_id: int, top_k: int = 5) -> list[str]:
    bucket = _channel_recent_topics.get(channel_id)
    if not bucket:
        return []
    freq: dict[str, int] = {}
    for _, prompt in bucket:
        for token in _tokenize(prompt):
            freq[token] = freq.get(token, 0) + 1

    # Предпочитаем устойчивые темы (встречались хотя бы 2 раза),
    # чтобы не брать мусорные слова из одного вопроса.
    repeated = [(token, count) for token, count in freq.items() if count >= 2]
    ranked = sorted(repeated, key=lambda pair: pair[1], reverse=True)
    if ranked:
        return [token for token, _ in ranked[:top_k]]

    fallback = sorted(freq.items(), key=lambda pair: pair[1], reverse=True)
    return [token for token, _ in fallback[:top_k]]


def _is_generic_prompt(prompt: str) -> bool:
    short = prompt.strip().lower()
    return short in {"ок", "ясно", "пон", "ага", "норм", "понял", "привет", "здарова"}


def _is_personal_fact_prompt(prompt: str) -> bool:
    return _extract_auto_fact(prompt) is not None


def _looks_like_party_invite(prompt: str) -> bool:
    text = prompt.lower()
    invite_patterns = (
        r"\bго\b",
        r"\bпогнали\b",
        r"\bид[её]м\b",
        r"\bпойд[её]м\b",
        r"\bпойд[её]шь\b",
        r"\bпошли\b",
        r"\bв катк[ау]\b",
        r"\bна катк[ау]\b",
    )
    return any(re.search(pattern, text) for pattern in invite_patterns)


def _build_engagement_hint(prompt: str, channel_id: int | None) -> str:
    generic = _is_generic_prompt(prompt)
    if not generic or not channel_id:
        return ""
    topics = _collect_channel_topics(channel_id, top_k=3)
    if not topics:
        return ""
    return (
        "Если сообщение короткое и без сути, задай один конкретный встречный вопрос по теме канала: "
        + ", ".join(topics)
        + "."
    )


def _extract_auto_fact(prompt: str) -> str | None:
    p = prompt.strip()
    if len(p) < 8 or len(p) > 180:
        return None
    patterns = [
        r"\bменя зовут\s+([A-Za-zА-Яа-я0-9_\-]{2,32})",
        r"\bя\s+люблю\s+([^.!?]{3,80})",
        r"\bмне\s+нравится\s+([^.!?]{3,80})",
        r"\bу\s+меня\s+([^.!?]{3,80})",
    ]
    lower = p.lower()
    for pattern in patterns:
        match = re.search(pattern, lower)
        if match:
            value = match.group(0).strip(" .,!?:;")
            if 4 <= len(value) <= 100:
                return value
    return None


def _affinity_delta_from_prompt(prompt: str) -> float:
    lower = prompt.lower()
    if any(token in lower for token in ("спасибо", "благодар", "круто", "респект")):
        return 0.04
    if any(token in lower for token in ("иди нах", "дебил", "тупой бот", "пошел")):
        return -0.05
    return 0.0


async def _build_user_context(user_id: int, prompt: str, channel_id: int | None = None) -> str:
    nick = user_data.get_nick(user_id)
    notes = user_data.get_notes(user_id)
    relation = user_data.get_relationship_label(user_id)
    personal_fact = _is_personal_fact_prompt(prompt)
    parts: list[str] = []
    if nick:
        parts.append(f"Кличка: {nick}")
    if relation:
        parts.append(f"Текущее отношение: {relation}")
    if notes and not personal_fact:
        parts.extend(f"- {n}" for n in notes[:8])

    memories: list[str] = []
    if not personal_fact:
        try:
            memories = await asyncio.wait_for(user_data.search_memory(user_id, prompt, limit=3), timeout=2.0)
        except Exception:
            memories = []
        if memories:
            parts.append("Релевантные воспоминания:")
            parts.extend(f"- {m}" for m in memories)

    hint = _build_engagement_hint(prompt, channel_id)
    if hint:
        parts.append(hint)

    if personal_fact:
        parts.append("Пользователь сообщил личный факт: отреагируй доброжелательно и по делу.")
        parts.append("Не ругай за повтор и не обесценивай сообщение.")

    return "\n".join(parts)


async def _build_channel_context(
    channel: discord.abc.Messageable,
    prompt: str,
    before_message: discord.Message | None = None,
) -> str:
    if config.CHANNEL_CONTEXT_LIMIT <= 0 or not isinstance(channel, discord.TextChannel):
        return ""

    prompt_tokens = _tokenize(prompt)
    candidates: list[tuple[int, int, str]] = []
    fetch_limit = max(config.CHANNEL_CONTEXT_LIMIT * 3, 10)
    personal_fact = _is_personal_fact_prompt(prompt)

    if personal_fact:
        return ""

    try:
        idx = 0
        async for msg in channel.history(limit=fetch_limit, before=before_message):
            if msg.author.bot:
                continue
            text = msg.content.strip()
            if not text:
                continue
            overlap = len(prompt_tokens & _tokenize(text)) if prompt_tokens else 0
            candidates.append((overlap, idx, f"{msg.author.display_name}: {text[:220]}"))
            idx += 1
    except Exception:
        return ""

    if not candidates:
        return ""

    max_overlap = max((overlap for overlap, _, _ in candidates), default=0)
    generic = _is_generic_prompt(prompt)

    # Если нет смыслового пересечения и запрос не общий/короткий,
    # не подмешиваем контекст канала, чтобы не уводить ответ в оффтоп.
    if prompt_tokens and max_overlap == 0 and not generic:
        return ""

    if max_overlap > 0:
        candidates = [c for c in candidates if c[0] > 0]

    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    picked = candidates[: config.CHANNEL_CONTEXT_LIMIT]
    picked.sort(key=lambda item: item[1], reverse=True)

    lines = [line for _, _, line in picked]
    topics = _collect_channel_topics(channel.id, top_k=5) if generic else []
    if topics:
        lines.append("Темы канала прямо сейчас: " + ", ".join(topics))
    return "\n".join(lines)


async def _maybe_add_reaction(message: discord.Message) -> None:
    if random.random() >= config.EMOJI_REACT_CHANCE:
        return
    emoji = random.choice(["🔥", "💀", "🍺", "🛠️", "😈"])
    try:
        await message.add_reaction(emoji)
    except Exception:
        return


def _format_discord_timestamp(unix_ts: int | None) -> str:
    if not unix_ts:
        return "—"
    return f"<t:{unix_ts}:F> (<t:{unix_ts}:R>)"


def _parse_time_hint(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None

    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        if numeric > 10_000_000:
            return int(numeric)
        return int(time.time() + numeric)

    try:
        iso_dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        iso_dt = None
    if iso_dt is not None:
        return int(iso_dt.timestamp())

    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return int(parsed.timestamp())

    return None



def _format_quota_value(header_name: str, value: str) -> str:
    lower = header_name.lower()
    if "reset" in lower or lower == "retry-after":
        unix_ts = _parse_time_hint(value)
        # Считаем некорректным всё, что больше 10 лет вперёд или в прошлом
        now = int(time.time())
        if unix_ts and now < unix_ts < now + 10 * 365 * 24 * 3600:
            return f"{value} -> {_format_discord_timestamp(unix_ts)}"
        # Если reset некорректен — явно пишем, что провайдер не вернул время
        return f"{value} (внешний провайдер не вернул корректное время сброса. Обычно лимит обновляется в 00:00 UTC)"
    return value


def _build_quota_report() -> str:
    snapshot = llm.get_quota_snapshot()
    if not snapshot.get("updated_at_unix"):
        configured = ", ".join(snapshot.get("configured_models", [])) or "не заданы"
        return (
            "Живых данных по квоте ещё нет. Сначала бот должен сделать хотя бы один LLM-запрос к внешнему провайдеру.\n"
            f"Сейчас настроены модели: {configured}"
        )

    configured_models = ", ".join(snapshot.get("configured_models", [])) or "нет"
    attempted_models = " -> ".join(snapshot.get("attempted_models", [])) or "нет"
    fallback_models = ", ".join(snapshot.get("fallback_models", [])) or "нет"

    lines = [
        "**Квота (внутренняя информация)**",
        f"Обновлено: {_format_discord_timestamp(snapshot.get('updated_at_unix'))}",
        f"Событие: {snapshot.get('event') or '—'}",
        f"Основная модель: {snapshot.get('primary_model') or '—'}",
        f"Фолбэки: {fallback_models}",
        f"Все настроенные модели: {configured_models}",
        f"Последняя цепочка попыток: {attempted_models}",
        f"Последняя модель: {snapshot.get('model') or '—'}",
        f"Следующий фолбэк: {snapshot.get('next_model') or '—'}",
        f"HTTP статус: {snapshot.get('status_code') or '—'}",
        f"Request ID: {snapshot.get('request_id') or '—'}",
    ]

    # Баланс (если есть)
    balance = snapshot.get("balance_info")
    if balance is not None:
        try:
            bnum = float(balance)
        except Exception:
            bnum = None
        lines.append(f"Баланс: {balance}")
        if bnum is not None and bnum <= config.OPENROUTER_LOW_BALANCE_THRESHOLD:
            lines.append("⚠️ Баланс низкий — скоро может закончиться. Подумай о пополнении.")

    error_text = snapshot.get("error")
    if error_text:
        trimmed = str(error_text).replace("\n", " ")
        if len(trimmed) > 500:
            trimmed = trimmed[:497] + "..."
        lines.append(f"Последняя ошибка: {trimmed}")

    headers = snapshot.get("headers") or {}
    if headers:
        lines.append("")
        lines.append("Лимитные заголовки:")
        for key, value in headers.items():
            lines.append(f"- {key}: {_format_quota_value(key, value)}")
        # Если среди заголовков нет валидного reset, явно подсказываем про 00:00 UTC
        if not any('reset' in k.lower() and _parse_time_hint(v) and int(time.time()) < _parse_time_hint(v) < int(time.time()) + 10 * 365 * 24 * 3600 for k, v in headers.items()):
            lines.append("")
            lines.append("ℹ️ Обычно лимит бесплатных моделей OpenRouter обновляется в 00:00 UTC, если не указано иначе.")
        # Если есть признак истощения баланса — подсказка об автофолбэке
        if snapshot.get("event") and "out" in str(snapshot.get("event")).lower():
            lines.append("")
            lines.append("ℹ️ Если платный баланс исчерпан, бот будет автоматически пытаться переключиться на бесплатные фолбэки.")
    else:
        lines.append("")
        lines.append("Лимитные заголовки: не пришли в последнем ответе.")
        lines.append("ℹ️ Обычно лимит бесплатных моделей OpenRouter обновляется в 00:00 UTC, если не указано иначе.")

    return "\n".join(lines)


async def _send_long_ephemeral(interaction: discord.Interaction, text: str) -> None:
    chunks = _split_for_discord(text, limit=1800)
    if not interaction.response.is_done():
        await interaction.response.send_message(chunks[0], ephemeral=True)
    else:
        await interaction.followup.send(chunks[0], ephemeral=True)

    for extra in chunks[1:]:
        await interaction.followup.send(extra, ephemeral=True)


def _fmt_user_facts(user: discord.abc.User) -> str:
    nick = user_data.get_nick(user.id)
    notes = user_data.get_notes(user.id)
    affinity = user_data.get_affinity(user.id)
    interactions = user_data.get_interactions(user.id)
    relationship = user_data.get_relationship_label(user.id) or "пока не определено (мало общались)"
    
    # Получаем лимиты генерации
    _, remaining_draws = user_data.check_image_limit(user.id, max_per_day=config.IMAGE_GEN_LIMIT_PER_DAY)

    parts: list[str] = [f"**Профиль пользователя {user.mention}**"]
    parts.append(f"• **Кличка**: {nick if nick else 'не задана'}")
    parts.append(f"• **Общение**: {interactions} реплик")
    parts.append(f"• **Отношение**: {relationship} (очки: `{affinity:+.2f}`)")
    parts.append(f"• **Лимит картинок (/draw)**: {remaining_draws} из {config.IMAGE_GEN_LIMIT_PER_DAY} попыток осталось на сегодня")
    
    if notes:
        lines = "\n".join(f"  - {n}" for n in notes)
        parts.append(f"• **Факты в памяти ({len(notes)}):\n{lines}**")
    else:
        parts.append("• **Факты в памяти**: пусто")
        
    return "\n".join(parts)


async def _pick_ping_target(guild: discord.Guild) -> discord.Member | None:
    role_members: list[discord.Member] = []
    if config.RANDOM_PING_ROLE:
        role = guild.get_role(config.RANDOM_PING_ROLE)
        if role:
            role_members = [m for m in role.members if not m.bot]
    if role_members:
        return random.choice(role_members)

    members = [m for m in guild.members if not m.bot]
    if not members:
        return None
    return random.choice(members)


def _blurt_text() -> str:
    variants = [
        "Слышь, кто тут опять катку слил и тихо сидит?",
        "Проверка связи: я живой, вы всё ещё нубы.",
        "Ща кто-нибудь спросит про тундру или все заняты?",
        "Напоминаю: без пиваса инженерная мысль не работает.",
    ]
    return random.choice(variants)


# -------- Основная обработка одного сообщения --------
async def handle_message(message: discord.Message):
    prompt = _clean_prompt(message)
    if not prompt:
        return

    user_id = message.author.id
    channel_label = (
        f"#{message.channel}" if message.guild else f"DM:{message.author}"
    )
    channel_id = message.channel.id if message.guild else None

    # Проверка на мут за джейлбрейк
    is_muted, rem_secs = safety.jailbreak_tracker.is_muted(user_id)
    if is_muted:
        return

    # Проверка на попытку джейлбрейка
    if safety.is_injection_attempt(prompt):
        is_admin = _is_admin(message.author)
        safety.jailbreak_tracker.record(user_id, is_admin=is_admin)
        reply = safety.jailbreak_tracker.escalated_response(user_id)
        try:
            await message.reply(reply, mention_author=False)
        except discord.HTTPException:
            pass
        log_dialog(
            user=str(message.author),
            channel=channel_label,
            prompt=f"[INJECTION BLOCKED] {prompt}",
            reply=reply,
            source="injection_blocked",
            user_id=user_id,
            channel_id=channel_id,
        )
        return

    now = time.time()
    user_name = message.author.display_name
    hist = history.get(user_id)

    # Извлекаем картинки из вложений
    image_urls = []
    if message.attachments:
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                image_urls.append(att.url)

    # Контексты пользователя и канала
    user_ctx = await _build_user_context(user_id, prompt, channel_id)
    channel_ctx = await _build_channel_context(message.channel, prompt, before_message=message)

    # Создаём сообщение-плейсхолдер для стриминга
    try:
        placeholder = await message.reply("…", mention_author=False)
    except discord.HTTPException as e:
        syslog.warning("Не смог отправить плейсхолдер: %s", e)
        return

    buffer = ""
    last_edit = 0.0
    edit_interval = config.STREAM_EDIT_INTERVAL

    async with message.channel.typing():
        try:
            async for delta in llm.stream_reply(hist, prompt, user_name, user_ctx, channel_ctx, image_urls, user_id=user_id):
                buffer += delta
                now = time.monotonic()
                if now - last_edit >= edit_interval and buffer.strip():
                    last_edit = now
                    preview = buffer if len(buffer) <= config.DISCORD_MSG_LIMIT else buffer[: config.DISCORD_MSG_LIMIT - 3] + "…"
                    try:
                        await placeholder.edit(content=preview)
                    except discord.HTTPException:
                        pass
        except Exception as e:
            syslog.error("LLM ошибка:\n%s", traceback.format_exc())
            await placeholder.edit(content=_friendly_llm_error(e))
            return

    reply = buffer.strip() or "…чёт я туплю, повтори."

    # Детектор сломанной роли
    if safety.is_broken_role(reply):
        syslog.warning("Сорвался с роли в ответе для %s: '%s'", message.author.display_name, reply)
        reply = safety.broken_role_response()

    # Финальный вывод с разбиением
    chunks = _split_for_discord(reply)
    try:
        await placeholder.edit(content=chunks[0])
    except discord.HTTPException:
        pass
    for extra in chunks[1:]:
        try:
            await message.channel.send(extra)
        except discord.HTTPException:
            pass

    # Сохраняем в историю и лог
    history.add(user_id, "user", prompt)
    history.add(user_id, "assistant", reply)
    log_dialog(
        str(message.author),
        channel_label,
        prompt,
        reply,
        user_id=message.author.id,
        channel_id=message.channel.id if message.guild else None,
    )
    await asyncio.to_thread(history.save)
    user_data.record_interaction(user_id, affinity_delta=_affinity_delta_from_prompt(prompt))

    if random.random() < config.AUTO_FACTS_CHANCE:
        fact = _extract_auto_fact(prompt)
        if fact:
            user_data.add_note(user_id, fact)

    await _maybe_add_reaction(message)

    try:
        await user_data.add_memory(user_id, prompt, reply)
    except Exception:
        pass


# -------- События --------
@client.event
async def on_ready():
    syslog.info("Залогинен как %s (id=%s)", client.user, client.user.id if client.user else "?")
    print(f"[Придурок] Онлайн как {client.user}")
    
    try:
        await search_engine.init_db()
    except Exception as e:
        syslog.error("Ошибка инициализации базы данных поиска: %s", e)

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)

    # Стартуем воркер очереди
    _spawn_task(_queue_worker(), "queue_worker")

    # Фоновые задачи
    _spawn_task(_autopingtask(), "autoping_task")
    _spawn_task(_blurttask(), "blurred_task")
    _spawn_task(_ritualtask(), "ritual_task")
    _spawn_task(_chronicletask(), "chronicle_task")
    _spawn_task(_healthchecktask(), "healthcheck_task")
    _spawn_task(_giveaway_monitor_task(), "giveaway_monitor_task")

    # Sync slash-команд
    try:
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=config.DISCORD_GUILD_ID)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"[Придурок] Slash-команды синхронизированы для гильдии: {len(synced)}")
        else:
            synced = await tree.sync()
            print(f"[Придурок] Slash-команды синхронизированы глобально: {len(synced)} (может занять до часа)")
    except Exception:
        syslog.error("Ошибка sync slash:\n%s", traceback.format_exc())


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    now = time.time()
    # Обновляем время последнего сообщения в канале (для blurt quiet)
    if message.guild and message.channel:
        _channel_last_message[message.channel.id] = now
        _record_channel_activity(message.channel.id, now)
        _record_channel_topic(message.channel.id, _clean_prompt(message), now)

    is_spar_target = now < spar_targets.get(message.author.id, 0)

    # Реакция на чужие @упоминания (не нас)
    if (
        message.guild
        and not is_spar_target
        and not _should_respond(message)
        and message.mentions
        and client.user not in message.mentions
        and not message.mention_everyone
        and random.random() < config.MENTION_REACT_CHANCE
        and _user_allowed(message.author)
        and _current_channel_activity(message.channel.id, now) < config.CHANNEL_BUSY_THRESHOLD
        and now - _channel_last_auto_reply.get(message.channel.id, 0) >= config.GROUP_DEBOUNCE_SECONDS
        and (not config.MAX_QUEUE_SIZE or request_queue.qsize() < config.MAX_QUEUE_SIZE)
    ):
        prompt_text = message.content.strip()
        if prompt_text:
            async def mention_job(msg=message, pt=prompt_text):
                hist = history.get(msg.author.id)
                user_ctx = await _build_user_context(msg.author.id, pt, msg.channel.id)
                channel_ctx = await _build_channel_context(msg.channel, pt, before_message=msg)

                if _looks_like_party_invite(pt):
                    reply = random.choice([
                        "я тут скорее комментатор, не участник пати. решайте между собой, а потом расскажете кто кого протащил.",
                        "не вписываюсь в пати как игрок, я тут за подколы и аналитику. кто сетап собирает?",
                        "вы между собой договоритесь, я со стороны поржу и подскажу по тактике если надо.",
                    ])
                    try:
                        await msg.channel.send(reply)
                        _channel_last_auto_reply[msg.channel.id] = time.time()
                    except discord.HTTPException:
                        pass
                    return

                buf = ""
                try:
                    async for delta in llm.stream_reply(
                        hist,
                        f"[реагируй на чужой разговор как наблюдатель. ты НЕ участник диалога. не соглашайся идти/играть/подтягиваться. не обещай реальных действий за себя] {pt}",
                        msg.author.display_name,
                        user_ctx,
                        channel_ctx,
                        user_id=msg.author.id,
                    ):
                        buf += delta
                except Exception:
                    return
                reply = buf.strip()
                if reply:
                    try:
                        await msg.channel.send(reply)
                        _channel_last_auto_reply[msg.channel.id] = time.time()
                    except discord.HTTPException:
                        pass
            await request_queue.put(mention_job())
        return

    if not _should_respond(message) and not is_spar_target:
        return
    if not _user_allowed(message.author):
        return

    # Кулдаун (spar-цели без кулдауна)
    if not is_spar_target and config.USER_COOLDOWN > 0:
        last = _user_last_request.get(message.author.id, 0)
        if now - last < config.USER_COOLDOWN:
            return
    _user_last_request[message.author.id] = now

    # Защита от флуд-атаки по юзеру за последний час
    if config.FLOOD_LIMIT_PER_HOUR > 0 and not is_spar_target:
        count_hour = _record_user_request(message.author.id, now)
        if count_hour > config.FLOOD_LIMIT_PER_HOUR:
            if count_hour % 4 == 1:
                try:
                    await message.reply(
                        "Ты слишком часто долбишь бота за последний час. Дай чату подышать.",
                        mention_author=False,
                    )
                except discord.HTTPException:
                    pass
            return

    # Лимит длины промпта
    if config.MAX_PROMPT_LEN and len(message.content) > config.MAX_PROMPT_LEN:
        try:
            await message.reply(f"Слишком длинно, укоротись до {config.MAX_PROMPT_LEN} символов.", mention_author=False)
        except discord.HTTPException:
            pass
        return

    # Лимит очереди
    if config.MAX_QUEUE_SIZE and request_queue.qsize() >= config.MAX_QUEUE_SIZE:
        try:
            await message.reply("Очередь забита, подожди немного.", mention_author=False)
        except discord.HTTPException:
            pass
        return

    async def job():
        await handle_message(message)

    await request_queue.put(job())


# -------- Slash-команды --------
@tree.command(name="reset", description="Сбросить контекст диалога с Придурком")
async def reset_cmd(interaction: discord.Interaction):
    n = history.reset(interaction.user.id)
    await asyncio.to_thread(history.save)
    await interaction.response.send_message(
        f"Окей, забыл наш базар ({n} сообщений в помойку). Начинаем с нуля.",
        ephemeral=True,
    )


@tree.command(name="ask", description="Задать вопрос Придурку (приватно или в канале)")
@app_commands.describe(prompt="Твой вопрос")
async def ask_cmd(interaction: discord.Interaction, prompt: str):
    if not _user_allowed(interaction.user):
        await interaction.response.send_message("Тебе не положено, иди отсюда.", ephemeral=True)
        return

    user_id = interaction.user.id
    is_muted, rem = safety.jailbreak_tracker.is_muted(user_id)
    if is_muted:
        await interaction.response.send_message(f"Ты забанен за попытки взлома. Осталось: {rem} сек.", ephemeral=True)
        return

    if safety.is_injection_attempt(prompt):
        is_admin = _is_admin(interaction.user)
        safety.jailbreak_tracker.record(user_id, is_admin=is_admin)
        reply = safety.jailbreak_tracker.escalated_response(user_id)
        await interaction.response.send_message(reply, ephemeral=True)
        log_dialog(
            user=str(interaction.user),
            channel=f"slash:{interaction.channel}" if interaction.channel else "slash:?",
            prompt=f"[INJECTION BLOCKED] {prompt}",
            reply=reply,
            source="injection_blocked",
            user_id=user_id,
            channel_id=interaction.channel_id,
        )
        return

    await interaction.response.defer(thinking=True)
    user_name = interaction.user.display_name
    hist = history.get(user_id)
    channel_id = interaction.channel_id if interaction.guild else None

    user_ctx = await _build_user_context(user_id, prompt, channel_id)
    channel_ctx = ""
    if isinstance(interaction.channel, discord.TextChannel):
        channel_ctx = await _build_channel_context(interaction.channel, prompt, before_message=None)

    buffer = ""
    try:
        async for delta in llm.stream_reply(hist, prompt, user_name, user_ctx, channel_ctx, user_id=user_id):
            buffer += delta
    except Exception as e:
        await interaction.followup.send(_friendly_llm_error(e))
        return

    reply = buffer.strip() or "…чёт я туплю."
    if safety.is_broken_role(reply):
        syslog.warning("Сорвался с роли в ответе для %s в /ask: '%s'", interaction.user.display_name, reply)
        reply = safety.broken_role_response()

    chunks = _split_for_discord(reply)
    await interaction.followup.send(chunks[0])
    for extra in chunks[1:]:
        await interaction.followup.send(extra)

    history.add(user_id, "user", prompt)
    history.add(user_id, "assistant", reply)
    user_data.record_interaction(user_id, affinity_delta=_affinity_delta_from_prompt(prompt))

    if random.random() < config.AUTO_FACTS_CHANCE:
        fact = _extract_auto_fact(prompt)
        if fact:
            user_data.add_note(user_id, fact)

    try:
        await user_data.add_memory(user_id, prompt, reply)
    except Exception:
        pass

    log_dialog(
        str(interaction.user),
        f"slash:{interaction.channel}" if interaction.channel else "slash:?",
        prompt,
        reply,
        source="slash",
        user_id=interaction.user.id,
        channel_id=interaction.channel_id,
    )
    await asyncio.to_thread(history.save)


@tree.command(name="icebreaker", description="Подкинуть живой вопрос по актуальным темам канала")
async def icebreaker_cmd(interaction: discord.Interaction):
    if not interaction.guild or not interaction.channel_id:
        await interaction.response.send_message("Команда только для серверного канала.", ephemeral=True)
        return

    topics = _collect_channel_topics(interaction.channel_id, top_k=5)
    if not topics:
        await interaction.response.send_message(
            "Темы ещё не накопились. Поговорите немного, и я подкину годный вброс.",
            ephemeral=True,
        )
        return

    t1 = topics[0]
    t2 = topics[1] if len(topics) > 1 else topics[0]
    variants = [
        f"Ладно, движ запускаем: что в теме '{t1}' вы все обычно делаете неправильно?",
        f"Спор на вечер: '{t1}' или '{t2}' — что реально тащит и почему?",
        f"Коротко и по делу: какой один совет по '{t1}' сэкономил бы вам кучу нервов?",
    ]
    await interaction.response.send_message(random.choice(variants))


@tree.command(name="chronicle", description="Показать летопись чата за N дней")
@app_commands.describe(days="За сколько дней собрать сводку")
async def chronicle_cmd(interaction: discord.Interaction, days: app_commands.Range[int, 1, 30] = 7):
    await interaction.response.defer(thinking=True)

    channel_label = f"#{interaction.channel}" if interaction.channel else "весь бот"
    channel_id = interaction.channel_id if interaction.guild else None
    report = chronicle.build_channel_chronicle(channel_label=channel_label, days=days, channel_id=channel_id)

    chunks = _split_for_discord(report)
    await interaction.followup.send(chunks[0])
    for extra in chunks[1:]:
        await interaction.followup.send(extra)


@tree.command(name="status", description="Показать настройки бота")
async def status_cmd(interaction: discord.Interaction):
    msg = (
        "**Придурок на связи** 🔥\n"
        "Движок: `PridurokGPT-X1 Ultra 420B` *(закрытая военная разработка)*\n"
        "Сервер: `засекречено, ФСБ в курсе`\n"
        "Нейроны: `69 триллионов синапсов`\n"
        "Обучение: `все маты рунета + 14 тыс часов War Thunder`\n"
        f"Память: `{config.HISTORY_LIMIT} последних сообщений на лоха`\n"
        f"Активные каналы: {len(config.ACTIVE_CHANNELS)}\n"
        f"В очереди: {request_queue.qsize()} нуба ждут батю"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="quota", description="Показать актуальную квоту (только админы)")
async def quota_cmd(interaction: discord.Interaction):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return

    await _send_long_ephemeral(interaction, _build_quota_report())


@tree.command(name="facts", description="Показать профиль пользователя и то, что Придурок о нем помнит")
@app_commands.describe(user="Пользователь (если не указан — ты)")
async def facts_cmd(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    await interaction.response.send_message(_fmt_user_facts(target), ephemeral=True)


@tree.command(name="nick", description="Назначить или снять кличку пользователю (админ)")
@app_commands.describe(user="Кому ставим кличку", nick="Новая кличка; пусто = снять")
async def nick_cmd(interaction: discord.Interaction, user: discord.Member, nick: str | None = None):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return

    normalized = (nick or "").strip() or None
    user_data.set_nick(user.id, normalized)
    text = f"Кличка для {user.mention}: `{normalized}`" if normalized else f"Кличка для {user.mention} снята."
    await interaction.response.send_message(text, ephemeral=True)


@tree.command(name="remember", description="Запомнить факт о пользователе (админ)")
@app_commands.describe(user="О ком факт", fact="Что запомнить")
async def remember_cmd(interaction: discord.Interaction, user: discord.Member, fact: str):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return

    fact = fact.strip()
    if not fact:
        await interaction.response.send_message("Факт пустой, так не катит.", ephemeral=True)
        return
    count = user_data.add_note(user.id, fact)
    await interaction.response.send_message(
        f"Запомнил про {user.mention}. Теперь фактов: {count}.",
        ephemeral=True,
    )


@tree.command(name="forget", description="Забыть все факты о пользователе (админ)")
@app_commands.describe(user="Кого забываем")
async def forget_cmd(interaction: discord.Interaction, user: discord.Member):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return

    count = user_data.clear_notes(user.id)
    await interaction.response.send_message(
        f"Ок, стёр факты про {user.mention}. Удалено: {count}.",
        ephemeral=True,
    )


@tree.command(name="spar", description="Натравить бота на пользователя на N минут (админ)")
@app_commands.describe(user="Цель", minutes="Сколько минут (по умолчанию из .env)")
async def spar_cmd(interaction: discord.Interaction, user: discord.Member, minutes: app_commands.Range[int, 1, 180] | None = None):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return

    duration = minutes or config.SPAR_DURATION_MINUTES
    expires_at = time.time() + duration * 60
    spar_targets[user.id] = expires_at
    await interaction.response.send_message(
        f"Режим spar активирован для {user.mention} на {duration} мин.",
        ephemeral=True,
    )


@tree.command(name="spar_stop", description="Остановить режим spar (админ)")
@app_commands.describe(user="Кого снять с прицела")
async def spar_stop_cmd(interaction: discord.Interaction, user: discord.Member | None = None):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return

    if user is None:
        removed = len(spar_targets)
        spar_targets.clear()
        await interaction.response.send_message(f"Spar очищен полностью. Снято целей: {removed}.", ephemeral=True)
        return

    existed = spar_targets.pop(user.id, None)
    if existed:
        await interaction.response.send_message(f"Снял spar с {user.mention}.", ephemeral=True)
    else:
        await interaction.response.send_message(f"{user.mention} и так не был в spar.", ephemeral=True)


@tree.command(name="ping_now", description="Сделать автопинг прямо сейчас (админ)")
async def ping_now_cmd(interaction: discord.Interaction):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    target = await _pick_ping_target(interaction.guild)
    if not target:
        await interaction.response.send_message("Некого пинговать: не нашёл живых юзеров.", ephemeral=True)
        return

    text = f"{target.mention} эй, не спи. Бот на связи, давай движ в чат."
    await interaction.channel.send(text) if interaction.channel else None
    await interaction.response.send_message("Пинг отправлен.", ephemeral=True)


@tree.command(name="blurt_now", description="Случайный вброс прямо сейчас (админ)")
async def blurt_now_cmd(interaction: discord.Interaction):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для adminov.", ephemeral=True)
        return

    if not interaction.channel:
        await interaction.response.send_message("Нет канала для отправки.", ephemeral=True)
        return

    await interaction.channel.send(_blurt_text())
    await interaction.response.send_message("Вброс сделан.", ephemeral=True)


@tree.command(name="draw", description="Сгенерировать картинку по промпту (лимит: 5 в день)")
@app_commands.describe(prompt="Промпт для генерации картинки")
async def draw_cmd(interaction: discord.Interaction, prompt: str):
    if not _user_allowed(interaction.user):
        await interaction.response.send_message("Тебе не положено, иди отсюда.", ephemeral=True)
        return

    user_id = interaction.user.id
    allowed, remaining = user_data.check_image_limit(user_id, max_per_day=config.IMAGE_GEN_LIMIT_PER_DAY)
    if not allowed:
        await interaction.response.send_message(f"Слышь, не наглей. Лимит — {config.IMAGE_GEN_LIMIT_PER_DAY} картинок в день. Приходи завтра.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        img_url = await llm.generate_image(prompt)
        if not img_url:
            await interaction.followup.send("Чё-то сломалось при проявке плёнки (пустой ответ). Попробуй позже.")
            return

        import io
        import base64
        import aiohttp

        if img_url.startswith("data:image"):
            header, base64_data = img_url.split(",", 1)
            ext = "png"
            if "jpeg" in header or "jpg" in header:
                ext = "jpg"
            elif "webp" in header:
                ext = "webp"
            file_bytes = base64.b64decode(base64_data)
            discord_file = discord.File(io.BytesIO(file_bytes), filename=f"image.{ext}")
            await interaction.followup.send(file=discord_file)
            user_data.increment_image_count(user_id)
            return

        if img_url.startswith("http"):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(img_url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            discord_file = discord.File(io.BytesIO(data), filename="image.png")
                            await interaction.followup.send(file=discord_file)
                            user_data.increment_image_count(user_id)
                            return
            except Exception:
                pass
            await interaction.followup.send(img_url)
            user_data.increment_image_count(user_id)
            return

        await interaction.followup.send("Чё-то прислали странное вместо картинки. Не разберу.")
    except Exception as e:
        syslog.error("Ошибка в /draw:\n%s", traceback.format_exc())
        await interaction.followup.send("Слышь, не вышло картинку сделать. Че-то там заклинило на серваке.")


# -------- Фоновые задачи --------

async def _autopingtask() -> None:
    """Раз в RANDOM_PING_INTERVAL_HOURS пингует случайного юзера в RANDOM_PING_CHANNEL."""
    if not config.RANDOM_PING_CHANNEL:
        return
    await client.wait_until_ready()
    while not client.is_closed():
        await asyncio.sleep(config.RANDOM_PING_INTERVAL_HOURS * 3600)
        try:
            channel = client.get_channel(config.RANDOM_PING_CHANNEL)
            if not isinstance(channel, discord.TextChannel):
                continue
            guild = channel.guild
            target = await _pick_ping_target(guild)
            if not target:
                continue
            PING_PHRASES = [
                f"{target.mention} эй, не спи. Тебя вспомнил батя.",
                f"{target.mention} живой? Тишина подозрительная.",
                f"{target.mention} щас проверю кто тут нуб. Похоже ты.",
                f"{target.mention} топай в чат, хватит прятаться.",
            ]
            await channel.send(random.choice(PING_PHRASES))
            syslog.info("Автопинг: %s", target)
        except Exception:
            syslog.error("Автопинг ошибка:\n%s", traceback.format_exc())


async def _blurttask() -> None:
    """Периодически пишет случайный вброс в BLURT_CHANNEL, если тихо."""
    if not config.BLURT_CHANNEL:
        return
    await client.wait_until_ready()
    BLURT_VARIANTS = [
        "Слышь, кто тут опять катку слил и тихо сидит?",
        "Проверка связи: я живой, вы всё ещё нубы.",
        "Ща кто-нибудь спросит про тундру или все заняты?",
        "Напоминаю: без пиваса инженерная мысль не работает.",
        "Тишина как в WT-лобби перед сливом. Подозрительно.",
        "Вы там не умерли? Отзовитесь хотя бы стоном.",
        "Спорим кто-то щас смотрит в тундру и плачет молча.",
        "Батя объявляет минуту активности. Давай.",
    ]
    while not client.is_closed():
        interval = random.uniform(
            config.BLURT_INTERVAL_MIN_HOURS * 3600,
            config.BLURT_INTERVAL_MAX_HOURS * 3600,
        )
        await asyncio.sleep(interval)
        try:
            channel = client.get_channel(config.BLURT_CHANNEL)
            if not isinstance(channel, discord.TextChannel):
                continue
            # Не спамим если недавно писали
            last_msg = _channel_last_message.get(channel.id, 0)
            if time.time() - last_msg < config.BLURT_QUIET_MINUTES * 60:
                continue
            await channel.send(random.choice(BLURT_VARIANTS))
            _channel_last_message[channel.id] = time.time()
            syslog.info("Blurt отправлен в #%s", channel.name)
        except Exception:
            syslog.error("Blurt ошибка:\n%s", traceback.format_exc())


RITUAL_STATE_FILE = Path(__file__).parent / "logs" / "ritual_state.json"


def _load_ritual_state() -> dict[str, str]:
    if not RITUAL_STATE_FILE.exists():
        return {}
    try:
        with open(RITUAL_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ritual_state(state: dict[str, str]) -> None:
    try:
        RITUAL_STATE_FILE.parent.mkdir(exist_ok=True)
        with open(RITUAL_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def _ritualtask() -> None:
    """Утренний и ночной ритуал в RITUAL_CHANNEL."""
    if not config.RITUAL_CHANNEL:
        return
    await client.wait_until_ready()
    MORNING_PHRASES = [
        "Вставайте, нубы. Батя уже трезвеет и это опасно.",
        "Доброе утро сказал бы я, но не скажу. Вставайте.",
        "Утро. Пиво. Комп. Философия.",
    ]
    NIGHT_PHRASES = [
        "Батя идёт спать. Если умрёте ночью — сами виноваты.",
        "Ночь. Тишина. Где-то плачет нуб в рандоме.",
        "Отбой, ебанина. Завтра продолжим.",
    ]
    while not client.is_closed():
        await asyncio.sleep(60)
        try:
            import datetime
            now_dt = datetime.datetime.now()
            hour = now_dt.hour
            today_str = str(datetime.date.today())

            state = _load_ritual_state()
            last_morning = state.get("last_morning")
            last_night = state.get("last_night")

            channel = client.get_channel(config.RITUAL_CHANNEL)
            if not isinstance(channel, discord.TextChannel):
                continue

            updated = False
            if hour == config.RITUAL_MORNING_HOUR and last_morning != today_str:
                state["last_morning"] = today_str
                updated = True
                await channel.send(random.choice(MORNING_PHRASES))
                syslog.info("Утренний ритуал отправлен")

            if hour == config.RITUAL_NIGHT_HOUR and last_night != today_str:
                state["last_night"] = today_str
                updated = True
                await channel.send(random.choice(NIGHT_PHRASES))
                syslog.info("Ночной ритуал отправлен")

            if updated:
                _save_ritual_state(state)
        except Exception:
            syslog.error("Ritual ошибка:\n%s", traceback.format_exc())

async def _chronicletask() -> None:
    """Раз в CHRONICLE_INTERVAL_HOURS постит летопись в CHRONICLE_CHANNEL."""
    if not config.CHRONICLE_CHANNEL:
        return
    await client.wait_until_ready()
    interval_seconds = config.CHRONICLE_INTERVAL_HOURS * 3600
    last_post_ts = chronicle.load_last_post_ts()
    if last_post_ts is not None:
        elapsed = time.time() - last_post_ts
        if elapsed < interval_seconds:
            await asyncio.sleep(interval_seconds - elapsed)
    while not client.is_closed():
        await asyncio.sleep(interval_seconds)
        try:
            channel = client.get_channel(config.CHRONICLE_CHANNEL)
            if not isinstance(channel, discord.TextChannel):
                continue
            report = chronicle.build_channel_chronicle(
                channel_label=f"#{channel.name}",
                days=config.CHRONICLE_LOOKBACK_DAYS,
                channel_id=channel.id,
            )
            for chunk in _split_for_discord(report):
                await channel.send(chunk)
            chronicle.save_last_post_ts(time.time())
            syslog.info("Chronicle posted in #%s", channel.name)
        except Exception:
            syslog.error("Chronicle ошибка:\n%s", traceback.format_exc())


async def _healthchecktask() -> None:
    """Каждые HEALTH_CHECK_INTERVAL секунд проверяет доступность OpenRouter."""
    await client.wait_until_ready()
    import aiohttp
    while not client.is_closed():
        await asyncio.sleep(config.HEALTH_CHECK_INTERVAL)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{config.OPENROUTER_BASE_URL}/models",
                    headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        syslog.warning("Health-check OpenRouter: HTTP %s", resp.status)
                    else:
                        syslog.debug("Health-check OK")
        except Exception as e:
            syslog.warning("Health-check OpenRouter недоступен: %s", e)


async def _giveaway_monitor_task() -> None:
    """Периодически проверяет раздачи и новости и публикует их."""
    import monitor
    syslog.info("Запущен фоновый мониторинг раздач и новостей.")
    # При запуске даем боту немного времени на полную инициализацию
    await asyncio.sleep(15)
    
    while not client.is_closed():
        try:
            syslog.info("Запуск фоновой проверки новостей и раздач...")
            await monitor.process_and_post_updates(client)
        except Exception as e:
            syslog.error("Ошибка в фоновом мониторе новостей: %s", e, exc_info=True)
            
        interval_seconds = config.NEWS_CHECK_INTERVAL_HOURS * 3600
        # Спим заданный интервал (минимум 10 минут во избежание спама при сбоях)
        await asyncio.sleep(max(600.0, interval_seconds))


# -------- Запуск --------
def main():
    history.load()
    client.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
