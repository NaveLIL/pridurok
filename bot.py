"""Discord-бот «Придурок» — мост между Discord и локальной LLM в LM Studio."""
import asyncio
import ctypes
import random
import re
import sys
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timedelta

import discord
from discord import app_commands

import config
import history
import llm
import metrics
import safety
import user_data
from dialog_logger import log_dialog, system as syslog

# -------- Защита от запуска двух копий --------
if sys.platform == "win32":
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, True, "PridurokDiscordBotMutex")
    if ctypes.windll.kernel32.GetLastError() == 183:
        print("[Придурок] Уже запущен в другом терминале! Завершаемся.")
        sys.exit(1)
else:
    import os as _os
    _LOCKFILE = "/tmp/pridurok_bot.lock"
    import fcntl as _fcntl
    _lock_fd = open(_LOCKFILE, "w")
    try:
        _fcntl.flock(_lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except BlockingIOError:
        print("[Придурок] Уже запущен в другом терминале! Завершаемся.")
        sys.exit(1)


# -------- Discord setup --------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True  # для активности юзеров

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# -------- Глобальное состояние --------
_processing: set[int] = set()
_last_request: dict[int, float] = {}

_spar_target: int | None = None
_spar_until: float = 0.0

_last_mention_react: float = 0.0

# Группировка коротких сообщений: user_id -> list of pending Messages
_pending_groups: dict[int, list[discord.Message]] = {}
_pending_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# Учёт активности канала (busy detector): channel_id -> deque[ts]
_channel_msg_times: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=50))

# Эмодзи для коротких реакций
_REACTION_EMOJIS = ["🤡", "💩", "🥱", "🤓", "🤦", "😒", "☠️", "🗿", "🙄"]
# Триггеры для замены ответа на эмодзи (короткие/бессмысленные сообщения)
_LOW_EFFORT_PATTERNS = re.compile(
    r"^(?:ок|ok|okay|\+1|лол|lol|кек|kek|ха+|ахах+|хех|пон|спс|nice|cool|жиза|true|факт|база)\s*[!?.]*$",
    re.IGNORECASE,
)

# Трекеры фоновых задач
_worker_task: asyncio.Task | None = None
_pinger_task: asyncio.Task | None = None
_blurter_task: asyncio.Task | None = None
_ritual_task: asyncio.Task | None = None
_healthcheck_task: asyncio.Task | None = None


# -------- Очередь --------
request_queue: "asyncio.Queue[asyncio.Task]" = asyncio.Queue()


async def _queue_worker():
    while True:
        job = await request_queue.get()
        try:
            await job
        except Exception:
            syslog.error("Ошибка в job:\n%s", traceback.format_exc())
            metrics.record_error()
        finally:
            request_queue.task_done()


# -------- Утилиты --------
def _split_for_discord(text: str, limit: int = config.DISCORD_MSG_LIMIT) -> list[str]:
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
    return True


def _channel_busy(channel_id: int) -> bool:
    """True если в канале >N сообщений за последнюю минуту."""
    now = time.time()
    times = _channel_msg_times[channel_id]
    recent = sum(1 for t in times if now - t < 60)
    return recent >= config.CHANNEL_BUSY_THRESHOLD


def _track_channel_activity(channel_id: int) -> None:
    _channel_msg_times[channel_id].append(time.time())


def _user_activity(member: discord.abc.User) -> str | None:
    """Извлекает текущую активность юзера (что играет/слушает)."""
    if not isinstance(member, discord.Member) or not member.activities:
        return None
    for act in member.activities:
        if isinstance(act, discord.Game):
            return f"играет в {act.name}"
        if isinstance(act, discord.Streaming):
            return f"стримит {act.name or 'что-то'}"
        if isinstance(act, discord.Spotify):
            return f"слушает {act.title} — {act.artist}"
        if isinstance(act, discord.Activity) and act.name:
            return f"{act.type.name if hasattr(act.type, 'name') else 'занимается'}: {act.name}"
    return None


def _should_respond(message: discord.Message) -> bool:
    if message.author.bot:
        return False
    if not message.content.strip() and not message.attachments and not message.stickers:
        return False
    # DM
    if message.guild is None:
        return True
    is_mention = bool(client.user and client.user.mentioned_in(message) and not message.mention_everyone)
    # /spar — отвечаем целевому юзеру
    if _spar_target == message.author.id and time.time() < _spar_until:
        return True
    # Активный канал
    in_active = message.channel.id in config.ACTIVE_CHANNELS
    # Busy-режим: если канал занят и нас не пингают — молчим
    if in_active and _channel_busy(message.channel.id) and not is_mention:
        return False
    if in_active:
        return True
    if is_mention:
        return True
    # Упоминание другого человека в активном канале — иногда встреваем
    if (
        in_active
        and message.mentions
        and any(not u.bot and u != client.user for u in message.mentions)
    ):
        global _last_mention_react
        if time.time() - _last_mention_react > 120 and random.random() < config.MENTION_REACT_CHANCE:
            _last_mention_react = time.time()
            return True
    return False


async def _fetch_channel_context(message: discord.Message, limit: int) -> list[str]:
    if limit <= 0 or message.guild is None:
        return []
    try:
        msgs = []
        async for m in message.channel.history(limit=limit + 1, before=message):
            if not m.content.strip():
                continue
            author = m.author.display_name
            text = m.content.replace("\n", " ").strip()
            if len(text) > 200:
                text = text[:200] + "…"
            msgs.append(f"{author}: {text}")
        msgs.reverse()
        return msgs
    except (discord.HTTPException, discord.Forbidden):
        return []


def _clean_prompt(message: discord.Message) -> str:
    text = message.content
    if client.user:
        text = text.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "")
    return text.strip()


def _detect_affinity_delta(prompt: str) -> float:
    """Грубая оценка тональности для сдвига отношения бота к юзеру."""
    p = prompt.lower()
    negative = ["долбоеб", "идиот", "тупой", "лох", "хуй", "пиздец", "заткни", "иди нахуй", "уебок", "ненавижу"]
    positive = ["спасибо", "круто", "топ", "классно", "люблю", "молодец", "красава", "респект", "лучший"]
    score = 0.0
    for w in negative:
        if w in p:
            # Парадокс: если юзер агрессивен — бот его НЕ ненавидит, а наоборот, начинает уважать
            score += 0.05
    for w in positive:
        if w in p:
            # Если юзер льстит — бот ему ещё сильнее не доверяет
            score -= 0.03
    return max(-0.2, min(0.2, score))


# -------- Reroll button (View) --------
class RerollView(discord.ui.View):
    def __init__(self, original_message: discord.Message):
        super().__init__(timeout=300)
        self.original = original_message
        self.used = False

    @discord.ui.button(label="🔄 ещё раз", style=discord.ButtonStyle.secondary)
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.used:
            await interaction.response.send_message("Уже было.", ephemeral=True)
            return
        # Только автор оригинального сообщения может реролить
        if interaction.user.id != self.original.author.id:
            await interaction.response.send_message("Не твой ответ — не трогай.", ephemeral=True)
            return
        self.used = True
        button.disabled = True
        await interaction.response.edit_message(view=self)
        # Перезапускаем обработку
        await request_queue.put(_handle_message_job(self.original, is_reroll=True))


# -------- Реакции эмодзи на короткие сообщения --------
async def _maybe_react_emoji(message: discord.Message) -> bool:
    """Пытается поставить эмодзи вместо генерации ответа. Returns True если отреагировал."""
    text = message.content.strip()
    is_low_effort = bool(_LOW_EFFORT_PATTERNS.match(text)) or len(text) <= 3
    if not is_low_effort:
        return False
    if random.random() > config.EMOJI_REACT_CHANCE:
        return False
    try:
        await message.add_reaction(random.choice(_REACTION_EMOJIS))
        metrics.record_reaction()
        return True
    except (discord.HTTPException, discord.Forbidden):
        return False


# -------- Auto-facts фоновое извлечение --------
async def _auto_facts_job(user_id: int, user_msg: str, bot_reply: str):
    if random.random() > config.AUTO_FACTS_CHANCE:
        return
    try:
        await user_data.add_memory(user_id, user_msg, bot_reply)
        syslog.info("mem0: память обновлена для %s", user_id)
    except Exception:
        syslog.error("mem0: ошибка сохранения памяти:\n%s", traceback.format_exc())


# -------- Основная обработка сообщения --------
async def handle_message(message: discord.Message, *, is_reroll: bool = False):
    raw_prompt = _clean_prompt(message)

    # Реакция на стикер/картинку без текста
    if not raw_prompt and (message.attachments or message.stickers):
        snarky = random.choice([
            "Картиночками отвечаешь? Говорить разучился, обезьяна?",
            "Стикеры это аргумент только для двенадцатилетних. А тебе сколько?",
            "Файлами кидаешься как мудак, словами — никак. Понятно всё с тобой.",
            "О, без слов общается. Эволюция, мать её.",
        ])
        try:
            await message.reply(snarky, mention_author=False)
        except discord.HTTPException:
            pass
        return

    if not raw_prompt:
        return

    # Защита от prompt injection
    if safety.is_injection_attempt(raw_prompt):
        is_adm = _is_admin(message.author)
        count = safety.jailbreak_tracker.record(message.author.id, is_admin=is_adm)
        muted, secs = safety.jailbreak_tracker.is_muted(message.author.id)
        if muted:
            # Мут: отвечаем раз и потом тихо игнорируем
            if count == safety.JailbreakTracker.MUT_THRESHOLD:  # только первый раз сообщаем
                try:
                    await message.reply(
                        f"Достал. Заблокирован на {secs // 60} мин. Хватит уже.",
                        mention_author=False,
                    )
                except discord.HTTPException:
                    pass
        else:
            try:
                await message.reply(
                    safety.jailbreak_tracker.escalated_response(message.author.id),
                    mention_author=False,
                )
            except discord.HTTPException:
                pass
        return

    user_id = message.author.id
    user_name = message.author.display_name
    channel_label = f"#{message.channel}" if message.guild else f"DM:{message.author}"

    hist = history.get(user_id)
    summary = history.get_summary(user_id)
    channel_context = await _fetch_channel_context(message, config.CHANNEL_CONTEXT_LIMIT)
    activity = _user_activity(message.author)

    try:
        placeholder = await message.reply("…", mention_author=False)
    except discord.HTTPException as e:
        syslog.warning("Не смог отправить плейсхолдер: %s", e)
        return

    buffer = ""
    last_edit = 0.0
    edit_interval = config.STREAM_EDIT_INTERVAL
    started = time.monotonic()

    async with message.channel.typing():
        try:
            async for delta in llm.stream_reply(
                hist, raw_prompt, user_name,
                user_id=user_id, channel_context=channel_context,
                user_activity=activity, summary=summary,
            ):
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
            metrics.record_error()
            try:
                await placeholder.edit(content=f"⚠️ ИИ отвалился: `{type(e).__name__}: {e}`")
            except discord.HTTPException:
                pass
            return

    reply = buffer.strip() or "…чёт я туплю, повтори."

    # ВЫХОДНОЙ ФИЛЬТР: если модель сорвалась с роли (мяу, *действия*, "я ИИ", код) —
    # подменяем на едкую заготовку и НЕ сохраняем в историю (чтоб не отравлять контекст).
    role_broken = safety.is_broken_role(reply)
    if role_broken:
        syslog.warning("Сломанная роль обнаружена в ответе для %s: %r", user_name, reply[:200])
        metrics.record_error()
        reply = safety.broken_role_response()
        try:
            await placeholder.edit(content=reply, view=None)
        except discord.HTTPException:
            pass
        # Сохраняем только запрос пользователя (как факт что он пытался джейлбрейкнуть)
        # но НЕ сохраняем сломанный ответ бота
        history.add(user_id, "user", raw_prompt)
        history.add(user_id, "assistant", reply)  # сохраняем уже едкую заготовку
        log_dialog(str(message.author), channel_label, raw_prompt, f"[BROKEN→REPLACED] {reply}")
        return

    # Финальный вывод
    chunks = _split_for_discord(reply)
    view = RerollView(message)
    try:
        await placeholder.edit(content=chunks[0], view=view)
    except discord.HTTPException:
        pass
    for extra in chunks[1:]:
        try:
            await message.channel.send(extra)
        except discord.HTTPException:
            pass

    # История + лог + метрики
    history.add(user_id, "user", raw_prompt)
    history.add(user_id, "assistant", reply)
    user_data.record_interaction(user_id, _detect_affinity_delta(raw_prompt))
    latency = time.monotonic() - started
    metrics.record_reply(user_id, latency, tokens=len(reply.split()))
    log_dialog(str(message.author), channel_label, raw_prompt, reply)

    # Фоновые задачи: auto-facts + suммаризация
    asyncio.create_task(_auto_facts_job(user_id, raw_prompt, reply))
    asyncio.create_task(llm.maybe_summarize(user_id))


async def _handle_message_job(message: discord.Message, *, is_reroll: bool = False):
    try:
        await handle_message(message, is_reroll=is_reroll)
    finally:
        _processing.discard(message.id)


# -------- Группировка коротких сообщений (debounce) --------
async def _grouped_handler(user_id: int, channel_id: int):
    """Ждёт окно debounce, потом обрабатывает все накопленные сообщения как одно."""
    await asyncio.sleep(config.GROUP_DEBOUNCE_SECONDS)
    async with _pending_locks[user_id]:
        msgs = _pending_groups.pop(user_id, [])
    if not msgs:
        return
    # Берём последнее сообщение как "якорь" для reply, склеиваем тексты
    last_msg = msgs[-1]
    if len(msgs) > 1:
        # Склеиваем content всех сообщений
        combined = "\n".join(_clean_prompt(m) for m in msgs if _clean_prompt(m))
        # Подменяем content (через прокси-объект через monkey-patch не делаем — просто используем оригинал и хак)
        # Создаём искусственный якорь: модифицируем content последнего сообщения через присвоение - НЕЛЬЗЯ.
        # Поэтому обрабатываем через обёртку: передадим первый prompt как сумма.
        last_msg.content = combined  # discord.Message.content — обычный атрибут, можно
    await handle_message(last_msg)


# -------- Фоновый пингер --------
_PING_TIMESTAMP_FILE = "last_ping.txt"


def _get_last_ping_time() -> float:
    try:
        with open(_PING_TIMESTAMP_FILE, "r") as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0.0


def _set_last_ping_time(ts: float) -> None:
    try:
        with open(_PING_TIMESTAMP_FILE, "w") as f:
            f.write(str(ts))
    except OSError:
        pass


async def _do_random_ping() -> str:
    channel = client.get_channel(config.RANDOM_PING_CHANNEL)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return f"канал {config.RANDOM_PING_CHANNEL} не найден"
    guild = channel.guild
    if config.RANDOM_PING_ROLE:
        role = guild.get_role(config.RANDOM_PING_ROLE)
        if role is None:
            return f"роль {config.RANDOM_PING_ROLE} не найдена"
        candidates = [m for m in role.members if not m.bot]
    else:
        candidates = [m for m in guild.members if not m.bot]
    if not candidates:
        return "нет кандидатов"
    victim = random.choice(candidates)

    async def job(victim=victim, channel=channel):
        try:
            prompt = (
                f"Придумай ОДНО короткое язвительное обращение к {victim.display_name}, "
                f"одной фразой, чтобы он зашёл в чат. Без эмодзи, без диалогов."
            )
            text = await llm.one_shot(prompt, max_tokens=80)
            text = (text or "Эй, чё пропал?").split("\n")[0][: config.DISCORD_MSG_LIMIT - 50]
            await channel.send(f"{victim.mention} {text}")
            metrics.record_ping()
            log_dialog("SYSTEM", f"#{channel}", f"random_ping:{victim}", text)
        except Exception:
            syslog.error("Ошибка пингера job:\n%s", traceback.format_exc())

    await request_queue.put(job())
    _set_last_ping_time(time.time())
    return f"пинг отправлен: {victim.display_name}"


async def _random_pinger():
    interval_sec = max(60.0, config.RANDOM_PING_INTERVAL_HOURS * 3600)
    await asyncio.sleep(30)
    while True:
        try:
            now = time.time()
            elapsed = now - _get_last_ping_time()
            if elapsed >= interval_sec:
                status = await _do_random_ping()
                syslog.info("Пингер: %s", status)
            sleep_for = max(300.0, interval_sec / 3 - elapsed)
            await asyncio.sleep(min(sleep_for, interval_sec))
        except Exception:
            syslog.error("Ошибка пингера:\n%s", traceback.format_exc())
            await asyncio.sleep(600)


# -------- Погода через wttr.in (без API-ключа) --------
async def _fetch_weather(city: str) -> str | None:
    """Получает текущую погоду через wttr.in JSON API. Возвращает None при любой ошибке."""
    try:
        import aiohttp
        url = f"https://wttr.in/{city}?format=j1"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    cur = data["current_condition"][0]
                    temp = cur["temp_C"]
                    feels = cur["FeelsLikeC"]
                    desc = cur["weatherDesc"][0]["value"]
                    return f"{temp}°C, {desc}, ощущается как {feels}°C"
    except Exception:
        pass
    return None


# -------- Случайные вбросы --------
_BLURT_PROMPTS = [
    "Брось в чат провокационную фразу, которая разозлит читающих. Без приветствий.",
    "Скажи что-нибудь желчное и саркастичное про тех, кто сидит в этом чате. Одна фраза.",
    "Вброс: какое-нибудь обидное обобщение про современных людей. Кратко, едко.",
    "Скажи что-то спорное и грубое, на что захочется ответить. Одна-две фразы.",
    "Пожалуйся на чат, что тут все тупые и никто ничего интересного не пишет. Кратко и зло.",
    "Расскажи короткую историю из жизни 'когда я был молодым' с подъёбкой современным.",
    "Выскажи мнение про War Thunder, чтобы все начали спорить. Одно предложение.",
]


async def _do_blurt() -> str:
    if not config.BLURT_CHANNEL:
        return "blurt_channel не задан"
    channel = client.get_channel(config.BLURT_CHANNEL)
    if channel is None:
        return "канал не найден"
    try:
        last_msg_time = 0.0
        async for m in channel.history(limit=1):
            last_msg_time = m.created_at.timestamp()
        if time.time() - last_msg_time < config.BLURT_QUIET_MINUTES * 60:
            return "в чате недавно писали — пропускаю"
    except (discord.HTTPException, discord.Forbidden):
        pass
    prompt = random.choice(_BLURT_PROMPTS)
    weather = await _fetch_weather(config.WEATHER_CITY)
    if weather:
        prompt += f" За окном сейчас: {weather} — можешь ввернуть погоду в тему если вписывается."

    async def job():
        try:
            text = await llm.one_shot(prompt, max_tokens=120)
            text = text.strip()
            if not text:
                return
            text = text[: config.DISCORD_MSG_LIMIT - 10]
            await channel.send(text)
            metrics.record_blurt()
            log_dialog("SYSTEM", f"#{channel}", f"blurt:{prompt[:30]}", text)
        except Exception:
            syslog.error("Ошибка вброса:\n%s", traceback.format_exc())

    await request_queue.put(job())
    return "вброс отправлен"


async def _blurter():
    await asyncio.sleep(60)
    while True:
        try:
            mn = max(0.1, config.BLURT_INTERVAL_MIN_HOURS)
            mx = max(mn, config.BLURT_INTERVAL_MAX_HOURS)
            sleep_h = random.uniform(mn, mx)
            await asyncio.sleep(sleep_h * 3600)
            status = await _do_blurt()
            syslog.info("Вбрасыватель: %s", status)
        except Exception:
            syslog.error("Ошибка blurter:\n%s", traceback.format_exc())
            await asyncio.sleep(600)


# -------- Утренний/ночной ритуал --------
_MORNING_PROMPTS = [
    "Скажи 'доброе утро мудаки' в своём стиле, кратко и зло.",
    "Поприветствуй чат как мужик с похмелья — одна фраза, желчно.",
    "Утреннее приветствие в стиле злого бати. Одна фраза.",
]
_NIGHT_PROMPTS = [
    "Скажи что пора спать, малолеткам в школу, а ты взрослый и можешь сидеть. Кратко.",
    "Полночь — попрощайся с чатом так, чтобы все обиделись. Одна фраза.",
    "Скажи что идёшь бухать, а они пусть сидят как лохи. Кратко.",
]


async def _send_ritual(channel_id: int, prompts: list[str]) -> None:
    channel = client.get_channel(channel_id)
    if channel is None:
        return

    async def job():
        try:
            text = await llm.one_shot(random.choice(prompts), max_tokens=100)
            text = text.strip()[: config.DISCORD_MSG_LIMIT - 10]
            if text:
                await channel.send(text)
                log_dialog("SYSTEM", f"#{channel}", "ritual", text)
        except Exception:
            syslog.error("Ошибка ритуала:\n%s", traceback.format_exc())

    await request_queue.put(job())


async def _ritual():
    """Раз в день в M часов и в N часов отправляет фразу."""
    await asyncio.sleep(60)
    while True:
        try:
            now = datetime.now()
            # Найти ближайший час из morning/night
            targets = []
            for hour in (config.RITUAL_MORNING_HOUR, config.RITUAL_NIGHT_HOUR):
                t = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                if t <= now:
                    t += timedelta(days=1)
                targets.append((t, hour))
            targets.sort()
            next_time, next_hour = targets[0]
            sleep_for = (next_time - now).total_seconds()
            await asyncio.sleep(max(1.0, sleep_for))
            if not config.RITUAL_CHANNEL:
                continue
            prompts = _MORNING_PROMPTS if next_hour == config.RITUAL_MORNING_HOUR else _NIGHT_PROMPTS
            await _send_ritual(config.RITUAL_CHANNEL, prompts)
            await asyncio.sleep(120)  # чтобы не зацикливаться на той же минуте
        except Exception:
            syslog.error("Ошибка ритуала:\n%s", traceback.format_exc())
            await asyncio.sleep(600)


# -------- Health-check LM Studio --------
async def _healthcheck():
    await asyncio.sleep(30)
    while True:
        try:
            ok = await llm.health_check()
            if ok:
                safety.lm_breaker.record_success()
            else:
                safety.lm_breaker.record_failure()
                syslog.warning("Health-check failed (breaker fails: %d)", safety.lm_breaker._fails)
        except Exception:
            syslog.error("Ошибка healthcheck:\n%s", traceback.format_exc())
        await asyncio.sleep(config.HEALTH_CHECK_INTERVAL)


# -------- События --------
@client.event
async def on_ready():
    global _worker_task, _pinger_task, _blurter_task, _ritual_task, _healthcheck_task
    syslog.info("Залогинен как %s (id=%s)", client.user, client.user.id if client.user else "?")
    print(f"[Придурок] Онлайн как {client.user}")

    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_queue_worker())
        print("[Придурок] Воркер очереди запущен")
    if config.RANDOM_PING_CHANNEL and (_pinger_task is None or _pinger_task.done()):
        _pinger_task = asyncio.create_task(_random_pinger())
        print(f"[Придурок] Пингер запущен (каждые {config.RANDOM_PING_INTERVAL_HOURS} ч)")
    if config.BLURT_CHANNEL and (_blurter_task is None or _blurter_task.done()):
        _blurter_task = asyncio.create_task(_blurter())
        print(f"[Придурок] Вбрасыватель запущен ({config.BLURT_INTERVAL_MIN_HOURS}-{config.BLURT_INTERVAL_MAX_HOURS} ч)")
    if config.RITUAL_CHANNEL and (_ritual_task is None or _ritual_task.done()):
        _ritual_task = asyncio.create_task(_ritual())
        print(f"[Придурок] Ритуал запущен ({config.RITUAL_MORNING_HOUR}:00 и {config.RITUAL_NIGHT_HOUR}:00)")
    if _healthcheck_task is None or _healthcheck_task.done():
        _healthcheck_task = asyncio.create_task(_healthcheck())
        print("[Придурок] Health-check LM Studio запущен")

    try:
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=config.DISCORD_GUILD_ID)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"[Придурок] Slash-команды синхронизированы для гильдии: {len(synced)}")
        else:
            synced = await tree.sync()
            print(f"[Придурок] Slash-команды синхронизированы глобально: {len(synced)}")
    except Exception:
        syslog.error("Ошибка sync slash:\n%s", traceback.format_exc())


@client.event
async def on_message(message: discord.Message):
    # Трекаем активность всех каналов (даже от ботов и не отвечаемых)
    if message.guild is not None and not message.author.bot:
        _track_channel_activity(message.channel.id)

    if not _should_respond(message):
        return
    if not _user_allowed(message.author):
        return
    if message.id in _processing:
        return

    # Мут за повторные попытки взлома — молча игнорируем
    if safety.jailbreak_tracker.is_muted(message.author.id)[0]:
        return

    # Эмодзи-реакция вместо ответа на короткие сообщения (бесплатно, без LLM)
    if await _maybe_react_emoji(message):
        return

    # Длина
    if len(message.content) > config.MAX_PROMPT_LEN:
        try:
            await message.reply(
                f"Покороче формулируй, лох. Максимум {config.MAX_PROMPT_LEN} символов.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return

    # Если у юзера уже есть pending-группа — это часть той же реплики, минуем кулдаун/флуд/очередь
    user_id = message.author.id
    if user_id in _pending_groups:
        async with _pending_locks[user_id]:
            if user_id in _pending_groups:  # double-check под локом
                _pending_groups[user_id].append(message)
                _processing.add(message.id)
                return

    # Anti-flood
    blocked, retry_in = safety.check_flood(message.author.id, config.FLOOD_LIMIT_PER_HOUR)
    if blocked:
        try:
            await message.reply(
                f"Ты заебал, флудер. Подожди {retry_in // 60} мин.",
                mention_author=False, delete_after=10,
            )
        except discord.HTTPException:
            pass
        return

    # Кулдаун
    now = time.monotonic()
    last = _last_request.get(message.author.id, 0.0)
    wait = config.USER_COOLDOWN - (now - last)
    if wait > 0:
        try:
            await message.reply(
                f"Подожди ещё {wait:.0f} сек, не долби попусту.",
                mention_author=False, delete_after=5,
            )
        except discord.HTTPException:
            pass
        return

    # Очередь
    if request_queue.qsize() >= config.MAX_QUEUE_SIZE:
        try:
            await message.reply(
                "Очередь забита, подождите пока я с другими разберусь.",
                mention_author=False, delete_after=5,
            )
        except discord.HTTPException:
            pass
        return

    _last_request[message.author.id] = now
    _processing.add(message.id)

    # Группировка: создаём новую pending-группу (старые ветки уходят в раннем return выше)
    async with _pending_locks[user_id]:
        _pending_groups[user_id] = [message]

    # Запускаем отложенную обработку (debounce)
    async def grouped():
        try:
            await _grouped_handler(user_id, message.channel.id)
        finally:
            _processing.discard(message.id)

    await request_queue.put(grouped())


# -------- Slash-команды --------
def _is_admin(user) -> bool:
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


@tree.command(name="reset", description="Сбросить контекст диалога с Придурком")
async def reset_cmd(interaction: discord.Interaction):
    n = history.reset(interaction.user.id)
    await interaction.response.send_message(
        f"Окей, забыл наш базар ({n} сообщений в помойку). Начинаем с нуля.",
        ephemeral=True,
    )


@tree.command(name="forget_me", description="Удалить все факты И историю про себя")
async def forget_me_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    n_hist = history.reset(interaction.user.id)
    n_facts = user_data.clear_notes(interaction.user.id)
    n_mem = await user_data.clear_memory(interaction.user.id)
    user_data.set_nick(interaction.user.id, None)
    await interaction.followup.send(
        f"Забыл про тебя всё: история ({n_hist}), факты ({n_facts}), "
        f"семантическая память ({n_mem} записей), кличка снята.",
        ephemeral=True,
    )


@tree.command(name="ping_now", description="[Админ] Запустить пинг рандомного юзера прямо сейчас")
async def ping_now_cmd(interaction: discord.Interaction):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return
    if not config.RANDOM_PING_CHANNEL:
        await interaction.response.send_message("Пингер не настроен в .env", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    status = await _do_random_ping()
    await interaction.followup.send(f"OK: {status}", ephemeral=True)


@tree.command(name="ask", description="Задать вопрос Придурку")
@app_commands.describe(prompt="Твой вопрос")
async def ask_cmd(interaction: discord.Interaction, prompt: str):
    if not _user_allowed(interaction.user):
        await interaction.response.send_message("Тебе не положено, иди отсюда.", ephemeral=True)
        return
    if safety.is_injection_attempt(prompt):
        await interaction.response.send_message(safety.injection_response(), ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    user_id = interaction.user.id
    user_name = interaction.user.display_name
    hist = history.get(user_id)
    summary = history.get_summary(user_id)
    buffer = ""
    started = time.monotonic()
    try:
        async for delta in llm.stream_reply(hist, prompt, user_name, user_id=user_id, summary=summary):
            buffer += delta
    except Exception as e:
        await interaction.followup.send(f"⚠️ ИИ отвалился: `{type(e).__name__}: {e}`")
        return
    reply = buffer.strip() or "…чёт я туплю."
    chunks = _split_for_discord(reply)
    await interaction.followup.send(chunks[0])
    for extra in chunks[1:]:
        await interaction.followup.send(extra)
    history.add(user_id, "user", prompt)
    history.add(user_id, "assistant", reply)
    user_data.record_interaction(user_id, _detect_affinity_delta(prompt))
    metrics.record_reply(user_id, time.monotonic() - started, tokens=len(reply.split()))
    log_dialog(str(interaction.user), f"slash:{interaction.channel}" if interaction.channel else "slash:?", prompt, reply)
    asyncio.create_task(_auto_facts_job(user_id, prompt, reply))


@tree.command(name="status", description="Показать настройки бота")
async def status_cmd(interaction: discord.Interaction):
    spar_str = "выкл"
    if _spar_target and time.time() < _spar_until:
        left = int((_spar_until - time.time()) / 60)
        spar_str = f"<@{_spar_target}> (осталось {left} мин)"
    breaker = "OPEN ⚠️ модель отвалилась" if safety.lm_breaker.is_open else "OK ✅"
    msg = (
        f"**Придурок на связи**\n"
        f"Модель: `{config.LMSTUDIO_MODEL}`\n"
        f"LM Studio: `{config.LMSTUDIO_BASE_URL}` → {breaker}\n"
        f"Аптайм: {metrics.uptime_str()}\n"
        f"История: {config.HISTORY_LIMIT} сообщений (персистентная + суммаризация)\n"
        f"В очереди: {request_queue.qsize()}/{config.MAX_QUEUE_SIZE}\n"
        f"Кулдаун: {config.USER_COOLDOWN} сек | Anti-flood: {config.FLOOD_LIMIT_PER_HOUR}/час\n"
        f"Автопинг: {'вкл, каждые ' + str(config.RANDOM_PING_INTERVAL_HOURS) + ' ч' if config.RANDOM_PING_CHANNEL else 'выкл'}\n"
        f"Вбросы: {'вкл, ' + str(config.BLURT_INTERVAL_MIN_HOURS) + '-' + str(config.BLURT_INTERVAL_MAX_HOURS) + ' ч' if config.BLURT_CHANNEL else 'выкл'}\n"
        f"Ритуал: {'вкл, ' + str(config.RITUAL_MORNING_HOUR) + ':00 / ' + str(config.RITUAL_NIGHT_HOUR) + ':00' if config.RITUAL_CHANNEL else 'выкл'}\n"
        f"/spar активен: {spar_str}"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="stats", description="Показать статистику работы бота")
async def stats_cmd(interaction: discord.Interaction):
    s = metrics.snapshot()
    top = metrics.top_users(5)
    top_lines = []
    for uid, n in top:
        nick = user_data.get_nick(uid) or f"<@{uid}>"
        top_lines.append(f"  {nick}: {n}")
    msg = (
        f"**Статистика Придурка**\n"
        f"Аптайм: {s['uptime']}\n"
        f"Ответов: {s['replies']} | ошибок: {s['errors']}\n"
        f"Вбросов: {s['blurts']} | пингов: {s['pings']} | реакций: {s['reactions']}\n"
        f"Уникальных юзеров: {s['unique_users']}\n"
        f"Средняя задержка: {s['avg_latency_s']} сек\n"
        f"Скорость: {s['avg_tok_s']} слов/сек\n\n"
        f"**Топ собеседников:**\n" + ("\n".join(top_lines) if top_lines else "  пока никого")
    )
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="nick", description="[Админ] Назначить кличку юзеру")
@app_commands.describe(user="Кому назначить", nickname="Кличка (пусто чтобы убрать)")
async def nick_cmd(interaction: discord.Interaction, user: discord.User, nickname: str = ""):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return
    if nickname.strip():
        user_data.set_nick(user.id, nickname.strip()[:50])
        await interaction.response.send_message(
            f"OK, теперь {user.mention} → «{nickname.strip()}»", ephemeral=True
        )
    else:
        user_data.set_nick(user.id, None)
        await interaction.response.send_message(
            f"OK, кличка для {user.mention} снята", ephemeral=True
        )


@tree.command(name="remember", description="[Админ] Запомнить факт про юзера")
@app_commands.describe(user="О ком факт", fact="Что запомнить")
async def remember_cmd(interaction: discord.Interaction, user: discord.User, fact: str):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return
    fact = fact.strip()[:300]
    if not fact:
        await interaction.response.send_message("Пустой факт.", ephemeral=True)
        return
    n = user_data.add_note(user.id, fact)
    await interaction.response.send_message(
        f"Запомнил про {user.mention}. Всего фактов: {n}", ephemeral=True
    )


@tree.command(name="forget", description="[Админ] Забыть все факты про юзера")
@app_commands.describe(user="О ком забыть")
async def forget_cmd(interaction: discord.Interaction, user: discord.User):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return
    n = user_data.clear_notes(user.id)
    await interaction.response.send_message(
        f"Забыл всё про {user.mention} ({n} фактов)", ephemeral=True
    )


@tree.command(name="facts", description="Посмотреть что бот помнит про юзера")
@app_commands.describe(user="О ком посмотреть (пусто = про себя)")
async def facts_cmd(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user
    nick = user_data.get_nick(target.id)
    notes = user_data.get_notes(target.id)
    aff = user_data.get_affinity(target.id)
    inter = user_data.get_interactions(target.id)
    rel = user_data.get_relationship_label(target.id)
    lines = [f"**Про {target.display_name}:**"]
    if nick:
        lines.append(f"Кличка: «{nick}»")
    lines.append(f"Взаимодействий: {inter} | Affinity: {aff:+.2f}")
    if rel:
        lines.append(f"Отношение: _{rel}_")
    if notes:
        lines.append("Факты:")
        for n in notes:
            lines.append(f"• {n}")
    if not nick and not notes and inter == 0:
        lines.append("_ничего не помню_")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="spar", description="[Админ] Натравить Придурка на конкретного юзера")
@app_commands.describe(user="Жертва", minutes="Длительность в минутах")
async def spar_cmd(interaction: discord.Interaction, user: discord.User, minutes: int = 0):
    global _spar_target, _spar_until
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return
    mins = max(1, min(minutes if minutes > 0 else config.SPAR_DURATION_MINUTES, 120))
    _spar_target = user.id
    _spar_until = time.time() + mins * 60
    await interaction.response.send_message(
        f"Натравил на {user.mention} на {mins} минут.", ephemeral=True,
    )


@tree.command(name="spar_stop", description="[Админ] Прекратить травлю")
async def spar_stop_cmd(interaction: discord.Interaction):
    global _spar_target, _spar_until
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return
    _spar_target = None
    _spar_until = 0.0
    await interaction.response.send_message("Травля остановлена.", ephemeral=True)


@tree.command(name="blurt_now", description="[Админ] Сделать случайный вброс прямо сейчас")
async def blurt_now_cmd(interaction: discord.Interaction):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return
    if not config.BLURT_CHANNEL:
        await interaction.response.send_message("BLURT_CHANNEL не задан в .env", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    status = await _do_blurt()
    await interaction.followup.send(f"OK: {status}", ephemeral=True)


@tree.command(name="unmute", description="[Админ] Снять мут обостренной защиты (JailbreakTracker) с юзера")
@app_commands.describe(user="С кого снять мут")
async def unmute_cmd(interaction: discord.Interaction, user: discord.Member):
    if not _is_admin(interaction.user):
        await interaction.response.send_message("Только для админов.", ephemeral=True)
        return
    if user.id in safety.jailbreak_tracker._muted_until:
        del safety.jailbreak_tracker._muted_until[user.id]
        if user.id in safety.jailbreak_tracker._attempts:
            safety.jailbreak_tracker._attempts[user.id].clear()
        syslog.info("Админ %s снял мут с %s", interaction.user, user.id)
        await interaction.response.send_message(f"Мут с {user.mention} снят. Счётчик обнулён.", ephemeral=False)
    else:
        await interaction.response.send_message(f"У {user.mention} и так нет мута.", ephemeral=True)


@tree.command(name="roast", description="Сжечь конкретного юзера в одном лютом сообщении")
@app_commands.describe(user="Кого жарить")
async def roast_cmd(interaction: discord.Interaction, user: discord.User):
    if not _user_allowed(interaction.user):
        await interaction.response.send_message("Тебе не положено.", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("Ботов не жарю.", ephemeral=True)
        return
    if user.id == interaction.user.id and not _is_admin(interaction.user):
        await interaction.response.send_message("Себя жарь у психотерапевта.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    nick = user_data.get_nick(user.id)
    notes = user_data.get_notes(user.id)
    facts_text = "\n".join(f"- {n}" for n in notes[-10:]) if notes else "ничего не знаю"
    extra = (
        f"Тебе нужно ОДНОЙ короткой фразой жёстко обоссать {user.display_name}. "
        f"Используй кличку '{nick}' если есть. Используй факты:\n{facts_text}\n"
        f"Без эмодзи, 1-3 предложения, максимально едко и обидно."
    )
    try:
        text = await llm.one_shot(f"Жарь {user.display_name}", system_extra=extra, max_tokens=200, temperature=1.0)
    except Exception as e:
        await interaction.followup.send(f"⚠️ ИИ сдох: {e}")
        return
    text = (text or "Чёт лень жарить, сам себя обоссы.")[: config.DISCORD_MSG_LIMIT - 50]
    await interaction.followup.send(f"{user.mention}\n{text}")


@tree.command(name="duel", description="Стравить двух юзеров — бот сгенерит обмен подъёбками")
@app_commands.describe(user1="Первый боец", user2="Второй боец")
async def duel_cmd(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
    if not _user_allowed(interaction.user):
        await interaction.response.send_message("Тебе не положено.", ephemeral=True)
        return
    if user1.id == user2.id or user1.bot or user2.bot:
        await interaction.response.send_message("Нужны два разных живых юзера.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    extra = (
        f"Сгенерируй короткий 2-раундовый обмен оскорблениями между {user1.display_name} и {user2.display_name}. "
        f"Формат СТРОГО:\n"
        f"{user1.display_name}: <фраза>\n"
        f"{user2.display_name}: <ответ>\n"
        f"{user1.display_name}: <добивка>\n"
        f"{user2.display_name}: <финальное унижение>\n"
        f"Каждая фраза 1-2 предложения, едкие и грубые."
    )
    try:
        text = await llm.one_shot("Дуэль", system_extra=extra, max_tokens=400, temperature=1.0)
    except Exception as e:
        await interaction.followup.send(f"⚠️ ИИ сдох: {e}")
        return
    if not text:
        await interaction.followup.send("Чёт никто не пришёл драться.")
        return
    # Победителя случайно
    winner = random.choice([user1, user2])
    msg = f"⚔️ **{user1.display_name} vs {user2.display_name}** ⚔️\n\n{text}\n\n🏆 По мнению Придурка побеждает {winner.mention}"
    chunks = _split_for_discord(msg)
    await interaction.followup.send(chunks[0])
    for extra_c in chunks[1:]:
        await interaction.followup.send(extra_c)


@tree.command(name="quote", description="Случайная цитата из истории чата с комментарием Придурка")
async def quote_cmd(interaction: discord.Interaction):
    if not _user_allowed(interaction.user):
        await interaction.response.send_message("Тебе не положено.", ephemeral=True)
        return
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("Только в канале сервера.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    candidates: list[discord.Message] = []
    try:
        async for m in interaction.channel.history(limit=300):
            if m.author.bot:
                continue
            if not m.content.strip():
                continue
            if len(m.content) < 15 or len(m.content) > 300:
                continue
            candidates.append(m)
    except (discord.HTTPException, discord.Forbidden):
        await interaction.followup.send("Не могу читать историю канала.")
        return
    if not candidates:
        await interaction.followup.send("Цитировать нечего, в чате пусто.")
        return
    quoted = random.choice(candidates)
    extra = (
        f"Прокомментируй едко эту цитату от {quoted.author.display_name}: «{quoted.content}». "
        f"1-2 предложения, желчно."
    )
    try:
        comment = await llm.one_shot("Прокомментируй", system_extra=extra, max_tokens=150, temperature=1.0)
    except Exception:
        comment = "Чёт даже комментировать лень."
    await interaction.followup.send(
        f"📜 **{quoted.author.display_name}** когда-то написал:\n> {quoted.content}\n\n💬 Придурок:\n{comment}"
    )


# -------- Запуск --------
def main():
    client.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
