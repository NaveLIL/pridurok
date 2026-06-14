import re
import json
import logging
import aiohttp
from bs4 import BeautifulSoup
from pathlib import Path

import user_data
import config
from search_engine import perform_search

_log = logging.getLogger("pridurok.tools")

def _resolve_user(identifier: str) -> tuple[int, dict] | None:
    # 1. Сначала ищем цифры (ID или <@ID>)
    digits = re.findall(r"\d+", str(identifier))
    if digits:
        uid = int(digits[0])
        with user_data._lock:
            # Если пользователя нет в базе, инициализируем его
            if str(uid) not in user_data._data:
                user_data._get_entry(uid)
            return uid, user_data._data[str(uid)]

    # 2. Ищем по кличке (nick) case-insensitive
    name_clean = str(identifier).lower().strip()
    with user_data._lock:
        for uid_str, entry in user_data._data.items():
            nick = entry.get("nick")
            if nick and str(nick).lower().strip() == name_clean:
                return int(uid_str), entry
    return None

async def web_search(query: str) -> str:
    """Поиск актуальной информации в интернете через поисковую систему."""
    try:
        _log.info("Инструмент web_search вызван с запросом: '%s'", query)
        res = await perform_search(query)
        return res or "Ничего не найдено."
    except Exception as e:
        _log.error("Ошибка в инструменте web_search: %s", e)
        return f"Ошибка при поиске: {e}"

async def read_url(url: str) -> str:
    """Прочитать и извлечь текстовое содержимое веб-страницы по указанному URL."""
    _log.info("Инструмент read_url вызван для: '%s'", url)
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status != 200:
                    return f"Ошибка загрузки URL: статус {response.status}"
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")
                
                # Удаляем скрипты и стили
                for element in soup(["script", "style", "meta", "noscript", "header", "footer"]):
                    element.extract()
                    
                text = soup.get_text(separator=" ")
                # Чистим лишние пробелы и пустые строки
                lines = (line.strip() for line in text.splitlines())
                chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                clean_text = "\n".join(chunk for chunk in chunks if chunk)
                
                if not clean_text:
                    return "Страница пустая или защищена от парсинга."
                return clean_text[:2000]
    except Exception as e:
        _log.error("Ошибка в инструменте read_url: %s", e)
        return f"Ошибка при чтении веб-страницы: {e}"

def get_user_info(discord_id_or_name: str) -> str:
    """Получить информацию о пользователе из базы данных (кличка, отношение, факты)."""
    _log.info("Инструмент get_user_info вызван для: '%s'", discord_id_or_name)
    res = _resolve_user(discord_id_or_name)
    if not res:
        return f"Пользователь '{discord_id_or_name}' не найден в локальной базе."
        
    uid, entry = res
    nick = entry.get("nick", "нет")
    affinity = entry.get("affinity", 0.0)
    relation = user_data.get_relationship_label(uid) or "нейтральное"
    notes = entry.get("notes", [])
    
    notes_str = "\n".join(f"- {n}" for n in notes) if notes else "нет фактов"
    return (
        f"ID: {uid}\n"
        f"Кличка: {nick}\n"
        f"Отношение: {relation} (affinity: {affinity})\n"
        f"Известные факты:\n{notes_str}"
    )

def remember_user_fact(discord_id_or_name: str, fact: str) -> str:
    """Запомнить важный новый факт о пользователе в его профиль/память."""
    _log.info("Инструмент remember_user_fact вызван для '%s': '%s'", discord_id_or_name, fact)
    res = _resolve_user(discord_id_or_name)
    if not res:
        return f"Пользователь '{discord_id_or_name}' не найден в локальной базе. Не удалось сохранить факт."
        
    uid, entry = res
    user_data.add_note(uid, fact)
    return f"Успешно запомнил факт для пользователя (ID {uid}): '{fact}'"

async def get_user_past_dialogues(discord_id_or_name: str, limit: int = 5) -> str:
    """Получить историю последних диалогов с пользователем за прошлые дни."""
    _log.info("Инструмент get_user_past_dialogues вызван для '%s', лимит=%d", discord_id_or_name, limit)
    log_file = Path(__file__).parent / "logs" / "dialog_events.jsonl"
    if not log_file.exists():
        return "История диалогов пуста (файл логов отсутствует)."

    res = _resolve_user(discord_id_or_name)
    target_id = res[0] if res else None
    
    # Если не нашли по ID, попробуем искать по переданной строке имени
    target_name = str(discord_id_or_name).lower().strip()

    matching_events = []
    try:
        with log_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
            
        for line in reversed(lines):
            if len(matching_events) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                evt_user_id = event.get("user_id")
                evt_user_name = str(event.get("user", "")).lower().strip()
                
                if target_id is not None:
                    if evt_user_id == target_id:
                        matching_events.append(event)
                else:
                    if evt_user_name == target_name:
                        matching_events.append(event)
            except Exception:
                continue
    except Exception as e:
        _log.error("Ошибка чтения логов диалогов: %s", e)
        return f"Ошибка при чтении истории диалогов: {e}"

    if not matching_events:
        return f"Не найдено прошлых диалогов для пользователя '{discord_id_or_name}'."

    output = []
    for event in reversed(matching_events):
        ts = event.get("ts", "")
        if "T" in ts:
            ts = ts.split("T")[0]
        prompt = event.get("prompt", "").replace("\n", " ")
        reply = event.get("reply", "").replace("\n", " ")
        output.append(f"[{ts}] Пользователь: {prompt}\n[{ts}] Придурок: {reply}")

    return "\n\n".join(output)

async def search_gif(query: str) -> str:
    """Поиск гифки по ключевым словам через GIPHY API."""
    api_key = config.GIPHY_API_KEY
    if not api_key:
        return "Не настроен GIPHY_API_KEY в конфигурации. Не могу прислать гифку."

    url = "https://api.giphy.com/v1/gifs/search"
    params = {
        "api_key": api_key,
        "q": query,
        "limit": 1,
        "rating": "pg-13",
        "lang": "ru"
    }
    try:
        _log.info("Инструмент search_gif вызван с запросом: '%s'", query)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as response:
                if response.status != 200:
                    return f"Ошибка API Giphy: статус {response.status}"
                data = await response.json()
                gifs = data.get("data", [])
                if gifs:
                    gif_id = gifs[0].get("id")
                    if gif_id:
                        return f"https://i.giphy.com/{gif_id}.gif"
                    gif_url = gifs[0].get("images", {}).get("original", {}).get("url")
                    if gif_url:
                        if "?" in gif_url:
                            gif_url = gif_url.split("?")[0]
                        return gif_url
                return "Гифка не найдена."
    except Exception as e:
        _log.error("Ошибка в инструменте search_gif: %s", e)
        return f"Ошибка при поиске гифки: {e}"


# Схемы инструментов для OpenRouter
OPENROUTER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск актуальной информации в интернете через DuckDuckGo. Используй для вопросов о новостях, фактах, погоде или событиях, о которых ты не знаешь.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос на русском или английском языке"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": "Прочитать текстовое содержимое веб-страницы по ссылке. Используй, если пользователь просит прочесть, пересказать или проанализировать статью/страницу.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Полный URL адрес веб-страницы"
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_info",
            "description": "Получить информацию о пользователе из базы данных (кличка, отношение бота, список известных фактов). Используй, если нужно вспомнить факты о собеседнике.",
            "parameters": {
                "type": "object",
                "properties": {
                    "discord_id_or_name": {
                        "type": "string",
                        "description": "Числовой Discord ID пользователя или его имя/никнейм"
                    }
                },
                "required": ["discord_id_or_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remember_user_fact",
            "description": "Запомнить важный новый факт о пользователе в его профиль/память. Используй, если пользователь прямо сообщает важную личную информацию или просит тебя запомнить что-то о нем.",
            "parameters": {
                "type": "object",
                "properties": {
                    "discord_id_or_name": {
                        "type": "string",
                        "description": "Числовой Discord ID пользователя или его имя/никнейм"
                    },
                    "fact": {
                        "type": "string",
                        "description": "Факт, который нужно запомнить (например: 'Любит играть на КВ-2', 'Живет во Владивостоке')"
                    }
                },
                "required": ["discord_id_or_name", "fact"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_past_dialogues",
            "description": "Получить переписку/диалоги с пользователем за прошлые дни. Используй, если пользователь спрашивает, о чем вы общались вчера, неделю назад, или вспоминает прошлые диалоги.",
            "parameters": {
                "type": "object",
                "properties": {
                    "discord_id_or_name": {
                        "type": "string",
                        "description": "Числовой Discord ID пользователя или его имя/никнейм"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Количество диалогов для извлечения (по умолчанию 5)",
                        "default": 5
                    }
                },
                "required": ["discord_id_or_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_gif",
            "description": "Поиск готовой анимации (GIF) по теме или эмоции через GIPHY API. Используй для иллюстрации шуток, реакций или по прямой просьбе дать гифку. НЕ используй для создания новых картинок, кастомных мемов или рисунков (для этого есть draw_illustration).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос для гифки на русском или английском (например: 'angry tankist', 'facepalm', 'victory')"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "draw_illustration",
            "description": "Сгенерировать смешную картинку-мем или иллюстрацию для ответа. Используй это в редких случаях, когда хочешь ответить пользователю визуальным мемом в стильном мультяшном/карикатурном стиле вместо текста. Промпт должен детально описывать сюжет картинки (например: 'ворчливый танкист в шлемофоне плачет перед сломанным компьютером, стиль карикатуры, смешной мем').",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Детальное описание сюжета картинки на русском или английском языке"
                    }
                },
                "required": ["prompt"]
            }
        }
    }
]

# Карта выполнения функций
async def execute_tool(name: str, arguments: dict) -> str:
    """Выполняет инструмент по имени и возвращает строковый результат."""
    try:
        if name == "web_search":
            return await web_search(arguments.get("query", ""))
        elif name == "read_url":
            return await read_url(arguments.get("url", ""))
        elif name == "get_user_info":
            return get_user_info(arguments.get("discord_id_or_name", ""))
        elif name == "remember_user_fact":
            return remember_user_fact(arguments.get("discord_id_or_name", ""), arguments.get("fact", ""))
        elif name == "get_user_past_dialogues":
            return await get_user_past_dialogues(
                arguments.get("discord_id_or_name", ""),
                arguments.get("limit", 5)
            )
        elif name == "search_gif":
            return await search_gif(arguments.get("query", ""))
        elif name == "draw_illustration":
            import llm
            res = await llm.generate_image(arguments.get("prompt", ""))
            return res or "Не удалось сгенерировать картинку."
        else:
            return f"Неизвестный инструмент: {name}"
    except Exception as e:
        _log.error("Исключение при выполнении инструмента %s: %s", name, e, exc_info=True)
        return f"Ошибка выполнения инструмента {name}: {e}"
