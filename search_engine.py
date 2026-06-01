import asyncio
import aiosqlite
from ddgs import DDGS

DB_FILE = "knowledge.db"

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS web_cache (query TEXT PRIMARY KEY, result TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.commit()

def _sync_search(query: str):
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=3))

async def perform_search(query: str) -> str:
    """Ищет в локальной базе. Если нет - гуглит, сохраняет и возвращает."""
    await init_db()
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT result FROM web_cache WHERE query=?", (query.lower().strip(),)) as cursor:
            row = await cursor.fetchone()
            if row:
                print(f"[Поиск] Найдено в локальном кэше: {query}")
                return row[0]
                
        print(f"[Поиск] Гуглим в интернете: {query}...")
        try:
            results = await asyncio.to_thread(_sync_search, query)
            if not results:
                return "Ничего не найдено в интернете."
            
            combined_info = "\n".join([f"- {r.get('body', '')}" for r in results])
            
            await db.execute("INSERT OR REPLACE INTO web_cache (query, result) VALUES (?, ?)", (query.lower().strip(), combined_info))
            await db.commit()
            
            return combined_info
        except Exception as e:
            print(f"Ошибка поиска: {e}")
            return ""
