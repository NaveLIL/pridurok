import json
import logging
import aiohttp
import xml.etree.ElementTree as ET
from pathlib import Path

import config
import llm
NEWS_SYSTEM_PROMPT = """Ты — Евгений Рикошетович Полторашкин (Придурок), саркастичный мужик 43 лет из Волгограда, бывший слесарь-сборщик, любитель танков (War Thunder). 
Твоя задача — прокомментировать и опубликовать новость из игрового мира или информацию о бесплатной раздаче игры в своем фирменном стиле (саркастичный, ворчливый, с матерком к месту).

Правила оформления сообщения (строго следуй им):
1. **Заголовок сообщения**: Начни сообщение с тематического эмодзи и жирного заголовка.
   - Для раздач Epic Games Store: **🎁 Бесплатная раздача: Название игры**
   - Для игровых новостей: **🎮 Новость: Заголовок новости**
2. **Пустая строка** после заголовка.
3. **Твой саркастичный комментарий**: Напиши 1-3 предложения в стиле Придурка. 
   - ВАЖНО: Весь комментарий должен быть оформлен как цитата Discord (символ `> ` в начале каждой строки с комментарием).
   - ВАЖНО: Разнообразь вступления! Избегай постоянного повторения "Слышь, аналитик" или "Слышь, лоботрясы". Пиши живее и каждый раз по-разному. Не задавай в конце поста вопросы аудитории.
4. **Пустая строка** после комментария.
5. **Ссылка на источник**: Оформи ссылку красиво с помощью Markdown (например: `👉 [Забрать халяву в EGS](ссылка)` или `👉 [Читать источник](ссылка)`).
6. **Разделитель**: В самом конце сообщения добавь разделительную линию `───────────────────` на отдельной строке.
"""

_log = logging.getLogger("pridurok.monitor")
DB_FILE = Path(__file__).parent / "logs" / "posted_news.json"

def _load_posted() -> set[str]:
    try:
        if DB_FILE.exists():
            with DB_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
    except Exception as e:
        _log.error("Ошибка при загрузке posted_news.json: %s", e)
    return set()

def _save_posted(posted_set: set[str]) -> None:
    try:
        DB_FILE.parent.mkdir(exist_ok=True)
        with DB_FILE.open("w", encoding="utf-8") as f:
            json.dump(list(posted_set), f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log.error("Ошибка при сохранении posted_news.json: %s", e)

async def check_egs_giveaways() -> list[dict]:
    url = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=ru&country=RU&allowCountries=RU"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    free_games = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status != 200:
                    _log.error("EGS API вернул статус: %d", response.status)
                    return []
                data = await response.json()
                
                elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
                for elem in elements:
                    title = elem.get("title")
                    price = elem.get("price", {})
                    total_price = price.get("totalPrice", {})
                    discount_price = total_price.get("discountPrice", 1)
                    
                    promotions = elem.get("promotions")
                    has_promo = False
                    if promotions and promotions.get("promotionalOffers"):
                        has_promo = True
                        
                    if discount_price == 0 and has_promo:
                        slug = elem.get("productSlug") or elem.get("urlSlug")
                        if not slug and elem.get("catalogNs", {}).get("mappings"):
                            mappings = elem.get("catalogNs", {}).get("mappings", [])
                            if mappings:
                                slug = mappings[0].get("pageSlug")
                                
                        link = f"https://store.epicgames.com/p/{slug}" if slug else "https://store.epicgames.com/"
                        desc = elem.get("description", "")
                        
                        free_games.append({
                            "title": title,
                            "link": link,
                            "description": desc,
                            "type": "egs"
                        })
    except Exception as e:
        _log.error("Ошибка парсинга EGS: %s", e, exc_info=True)
    return free_games

async def check_game_news() -> list[dict]:
    # Используем Playground RSS
    url = "https://www.playground.ru/rss/news.xml"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    news_items = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status != 200:
                    _log.error("RSS Playground вернул статус: %d", response.status)
                    return []
                xml_data = await response.text()
                
                root = ET.fromstring(xml_data)
                for item in root.findall(".//item")[:10]:
                    title = item.find("title")
                    link = item.find("link")
                    description = item.find("description")
                    
                    title_text = title.text.strip() if title is not None and title.text else ""
                    link_text = link.text.strip() if link is not None and link.text else ""
                    desc_text = description.text.strip() if description is not None and description.text else ""
                    
                    if title_text and link_text:
                        news_items.append({
                            "title": title_text,
                            "link": link_text,
                            "description": desc_text,
                            "type": "news"
                        })
    except Exception as e:
        _log.error("Ошибка парсинга RSS Playground: %s", e)
    return news_items

async def is_link_valid(url: str) -> bool:
    """Проверяет ссылку на валидность (структура + проверка на 404)."""
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False
        
        path = parsed.path.lower()
        # Проверяем на явные технические артефакты пустых путей или None/null
        if path.endswith("/p/") or path.endswith("/p/none") or path.endswith("/p/null"):
            return False
        
        segments = [s for s in path.split("/") if s]
        if "none" in segments or "null" in segments:
            return False

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.head(url, headers=headers, allow_redirects=True, timeout=5) as resp:
                    if resp.status == 404:
                        return False
                    if resp.status < 400 or resp.status in (403, 401, 503):
                        return True
            except Exception:
                pass
            
            async with session.get(url, headers=headers, allow_redirects=True, timeout=5) as resp:
                if resp.status == 404:
                    return False
                return True
    except Exception as e:
        _log.warning("Ошибка при валидации ссылки %s: %s", url, e)
        return True

async def process_and_post_updates(client) -> None:
    """Проверяет обновления и публикует новые в указанный канал."""
    if not config.GIVEAWAY_CHANNEL_ID:
        return

    channel = client.get_channel(config.GIVEAWAY_CHANNEL_ID)
    if not channel:
        try:
            channel = await client.fetch_channel(config.GIVEAWAY_CHANNEL_ID)
        except Exception as e:
            _log.error("Не удалось найти канал новостей %s: %s", config.GIVEAWAY_CHANNEL_ID, e)
            return

    posted = _load_posted()
    
    # 1. Проверяем раздачи EGS
    egs_games = await check_egs_giveaways()
    new_egs = [g for g in egs_games if g["link"] not in posted]
    
    # 2. Проверяем новости Playground
    news_items = await check_game_news()
    
    # Фильтруем новости по ключевым играм комьюнити (War Thunder, Minecraft, Squad)
    allowed_keywords = ["war thunder", "minecraft", "майнкрафт", "squad"]
    filtered_news = []
    for n in news_items:
        title_lower = n["title"].lower()
        desc_lower = n["description"].lower()
        if any(kw in title_lower or kw in desc_lower for kw in allowed_keywords):
            filtered_news.append(n)
            
    new_news = [n for n in filtered_news if n["link"] not in posted][:2]
    
    updates = new_egs + new_news
    if not updates:
        return

    _log.info("Найдено %d новых обновлений для отправки.", len(updates))
    
    for item in updates:
        title = item["title"]
        link = item["link"]
        desc = item["description"]
        itype = item["type"]
        
        # Валидация ссылки перед отправкой
        if not await is_link_valid(link):
            _log.warning("Ссылка не прошла валидацию: %s. Пропускаем новость '%s'.", link, title)
            posted.add(link)
            _save_posted(posted)
            continue
        
        # Рерайтим новость с помощью LLM
        prompt = ""
        if itype == "egs":
            prompt = (
                f"Напиши пост о бесплатной раздаче игры '{title}' на Epic Games Store.\n"
                f"Описание игры: {desc}\n"
                f"Ссылка на раздачу: {link}\n"
                f"Сформируй ответ строго по правилам оформления сообщения из системного промпта."
            )
        else:
            prompt = (
                f"Напиши пост об игровой новости: '{title}'.\n"
                f"Описание новости: {desc}\n"
                f"Ссылка на источник: {link}\n"
                f"Сформируй ответ строго по правилам оформления сообщения из системного промпта."
            )
            
        messages = [
            {"role": "system", "content": NEWS_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        
        try:
            rewritten_text = await llm.generate_response(messages)
            if rewritten_text:
                await channel.send(rewritten_text)
                posted.add(link)
                _save_posted(posted)
                _log.info("Новость успешно отправлена и сохранена в кэш: %s", link)
        except Exception as e:
            _log.error("Ошибка рерайта/отправки новости: %s", e)
