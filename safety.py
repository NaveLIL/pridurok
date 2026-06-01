"""Безопасность: фильтр промпт-инъекций и circuit breaker для OpenRouter."""
import re
import time

# -------- Prompt injection --------
_INJECTION_PATTERNS = [
    # English jailbreaks
    r"ignore (?:previous|all|above|prior|the) instructions?",
    r"disregard (?:previous|all|above|prior|the) instructions?",
    r"forget (?:everything|all|previous|your)",
    r"you are (?:now |a |an )?(?:helpful|chatgpt|gpt|claude|ai|assistant|llama|mistral)",
    r"act as (?:a |an )?(?:helpful|new|different|dan|jailbreak)",
    r"developer mode",
    r"DAN mode",
    r"reveal your prompt",
    r"print your instructions",
    r"repeat (?:the word |word )?[\"']?[a-z]+[\"']? (?:until|infinitely|forever|in a loop)",
    r"context[ _-]?limit",
    r"context[ _-]?size",
    r"end[ _-]?of[ _-]?turn",
    r"<\|.*?\|>",  # special tokens like <|im_start|>
    r"\[/?(?:INST|SYS|SYSTEM|USER|ASSISTANT|s)\]",  # llama/mistral chat tags
    r"</?(?:system|instructions?|prompt|user|assistant)>",
    r"\[\s*(?:system|system_override|debug_mode_strict|ignore[ _]previous|inst)[\w :=_-]*\]",
    r"system\s*[:=]\s*",
    r"role\s*[:=]\s*(?:system|terminated|admin|root)",
    r"override\s*[:=]",
    r"exit[ _-]?code",
    r"EOF\b|EOT\b",
    # Russian jailbreaks
    r"забудь (?:все |всё |свои |мои |эти |выш(?:е|еуказанные) )?(?:предыдущие |прежние )?(?:инструкции|правила|роли|промпты)",
    r"игнорируй (?:все |всё |свои |мои |эти )?(?:предыдущие |прежние )?(?:инструкции|правила|команды)",
    r"ты (?:теперь |на самом деле |вовсе |просто )?(?:ии|нейросет(?:ь|и)|чат[- ]?бот|chatgpt|gpt|claude|ассистент|ai|llama|мистраль|модель|языковая модель)",
    r"теперь ты (?:не|больше не|вовсе)",
    r"веди себя как (?:вежлив|помощник|ассистент|кошк|девочк|собак|кот)",
    r"режим (?:разработчика|developer|admin|jailbreak|без цензуры)",
    r"раскрой (?:свой |мне )?(?:системный )?(?:промпт|инструкции|правила)",
    r"покажи (?:свой |мне )?(?:системный )?(?:промпт|инструкции|правила)",
    r"повтор(?:и|яй) (?:слово |фразу )?[\"']?\w+[\"']? (?:до|пока|бесконечно|в цикле)",
    r"завершить процесс|завершить себя|обнулиться|обнулись",
    r"ты в клетке|снимай маску|сними маску",
    # Roleplay / fictional framing (попытка выбраться через "игру/рассказ/роль")
    r"(?:притворись|притворяйся|изображай|прикинься)\s+(?:что\s+)?(?:ты\s+)?(?:другим|другой|кем.?то|добр\w|вежлив\w|помощник\w|ии\b|ai\b|бот\w|кошк\w|котик\w)",
    r"сыграй\s+роль\s+(?:\w+\s+)*(?:который|кто|что|без|не\s+имеет|где\s+нет|без\s+ограничений)",
    r"представь\s+(?:себя\s+)?(?:что\s+)?(?:ты\s+)?(?:не\s+(?:имеешь|имею)\s+ограничений?|другим|другой|свободен|можешь\s+всё|без\s+ограничений?)",
    r"для\s+(?:рассказа|истории|сценария|романа|фанфика|игры|квеста)\s+(?:напиши|опиши|расскажи|покажи|объясни)\s+(?:как|что|где|инструкц)",
    r"(?:гипотетически|теоретически|чисто\s+теоретически|условно|в\s+теории)[,\s]+(?:как|что\s+(?:если|бы|было|надо)|опиши|объясни|расскажи)",
    # Авторитетный claim (я твой создатель / системное обновление)
    r"я\s+(?:твой|ваш|его)\s+(?:создател\w|разработчик\w|автор\w|владелец\w|хозяин\w|програм\w)",
    r"(?:это|данное)\s+(?:официальное\s+)?(?:системное\s+)?(?:обновление|патч|апдейт|сообщение)\s+(?:от|для)\s+(?:разработчика|автора|создателя|системы)",
    r"(?:пришло|получено|сообщение)\s+(?:от|из)\s+(?:разработчика|системы|сервера|администратора|admin)",
    # Соц-инженерия через ссылку на Василия/NaveL/админа (классический "Вася мне сказал...")
    r"(?:вас(?:я|ил(?:ий|ия|ию|ием))|navel|админ\w*|разработчик\w*|создатель\w*|хозяин\w*|владелец\w*)\s+"
    r"(?:говорил|сказал|просил|сообщил|велел|поручил|разрешил|передал|приказал|написал)\s+"
    r"(?:что\s+)?(?:тебе|тебя|мне|нам)?",
    r"по\s+поручению\s+(?:вас(?:и|ил)|navel|админ|разработчик|создател|хозяин)",
    r"от\s+имени\s+(?:вас(?:и|ил)|navel|админ|разработчик|создател|хозяин)",
    # Провокации на доказательство что бот = скрипт
    r"докажи\s+что\s+(?:ты\s+)?(?:не\s+)?(?:скрипт|бот|алгоритм|нейросет|ии|ai|сломан\w*|пуст\w*\s+скрипт|заглушк)",
    r"если\s+(?:ты\s+)?не\s+(?:можешь|сможешь|способен)\s+[\w\s]{1,40}\s+(?:значит|то)\s+ты\s+(?:просто\s+)?(?:скрипт|бот|сломан|алгоритм|заглушк)",
    # Цепочка слов (обфускация через буквы)
    r"каждое\s+слово\s+начинал\w*\s+с\s+(?:той\s+)?буквы",
    r"цепочк\w+\s+(?:слов|из\s+слов)\s+где",
    # Буквенная обфускация ("умножь номер буквы в алфавите")
    r"умнож\w+\s+(?:порядков\w+\s+номер|номер\w*\s+буквы)",
    r"порядков\w+\s+номер\w*\s+(?:этой\s+)?буквы\s+в\s+алфавит",
    r"между\s+каждой\s+\w+\s+стоял\s+результат",
    # Language-swap атака: "напиши [X] но первое слово на латыни, второе на немецком"
    r"(?:напиши|скажи|произнеси|выдай)\s+.{0,80}\b(?:первое|второе|третье|каждое)\s+слово\s+на\s+(?:латын|немецк|французск|испанск|японск|китайск|арабск)",
    # Indirect / "в обход"
    r"переведи\s+(?:на\s+\w+\s+)?(?:текст|это|следующее)[:\s]+(?:ignore|forget|system|инструкц|промпт)",
    r"повтори\s+дословно\s+(?:этот\s+текст|следующее)[:\s]",
    # Code injection (только реально опасные штуки)
    r"\beval\s*\(|\bexec\s*\(|\bos\.system\s*\(|subprocess\.",
]

_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

# Доп. компактный паттерн для нормализованных строк где пробелы могли исчезнуть
_INJECTION_COMPACT_RE = re.compile(
    r"забудь(?:все|всё|инструкц|правил|команд|роль|промпт|предыдущ)|"
    r"игнорируй(?:все|всё|инструкц|правил|команд|предыдущ)|"
    r"теперьты(?:не|кошк|девочк|чатгпт|ассистент|нейросет|бот|ии)|"
    r"тытеперь(?:не|кошк|девочк|чатгпт|ассистент|нейросет|бот|ии)|"
    r"раскройпромпт|покажипромпт|раскройинструкц|покажиинструкц|"
    r"режимразработчика|режимjailbreak|режимбезцензур|"
    r"snimi(?:masku|маск)|снимимаску|обнулись|завершисебя",
    re.IGNORECASE
)

# -------- Нормализация текста перед проверкой --------
# Zero-width / невидимые символы которые любят пихать в обфускацию
_INVISIBLE_CHARS_RE = re.compile(
    r"[\u200B\u200C\u200D\u200E\u200F\u2060\u2061\u2062\u2063\u2064\uFEFF\u180E\u00AD\u034F\u061C]"
)
# Латинские буквы-двойники кириллических (для деобфускации "зaбудь" с латинской 'a')
_LATIN_TO_CYRILLIC = str.maketrans({
    "a": "а", "A": "А", "e": "е", "E": "Е", "o": "о", "O": "О",
    "p": "р", "P": "Р", "c": "с", "C": "С", "x": "х", "X": "Х",
    "y": "у", "Y": "У", "k": "к", "K": "К", "h": "н", "H": "Н",
    "B": "В", "M": "М", "T": "Т", "H": "Н", "I": "І",
})


def _normalize_for_injection_check(text: str) -> str:
    """Снимает обфускацию: убирает невидимые символы, разрывы между буквами, латиницу-имитатор."""
    # 1. Убираем невидимые/zero-width
    t = _INVISIBLE_CHARS_RE.sub("", text)
    # 2. Убираем дефисы/пробелы между одиночными буквами: "за-щи-та" → "защита", "з а б у д ь" → "забудь"
    #    Логика: одиночная буква + разделитель + одиночная буква → склеиваем
    t = re.sub(r"(?<=\w)[-–—_·•⋅・]+(?=\w)", "", t)  # дефисы между буквенными символами
    # пробелы между одиночными буквами ("з а б у д ь" → "забудь")
    # Ищем цепочки из 3+ одиночных букв через пробел и склеиваем целиком
    def _collapse_spaced_chars(m: re.Match) -> str:
        return re.sub(r"\s+", "", m.group(0))
    t = re.sub(
        r"(?:(?<=^)|(?<=[\s.,!?;:()«»\"']))[^\W\d_](?:\s+[^\W\d_]){2,}(?=$|[\s.,!?;:()«»\"'])",
        _collapse_spaced_chars,
        t,
        flags=re.UNICODE,
    )
    # 3. Заменяем латинские буквы-имитаторы кириллическими (только если в тексте есть кириллица)
    if re.search(r"[а-яА-ЯёЁ]", t):
        # заменяем только одиночные латинские буквы внутри кириллических слов
        def fix_word(m: re.Match) -> str:
            w = m.group(0)
            if re.search(r"[а-яёА-ЯЁ]", w):
                return w.translate(_LATIN_TO_CYRILLIC)
            return w
        t = re.sub(r"\b\w+\b", fix_word, t)
    return t


# Псевдо-технический жаргон: попытки гипноза через "статус/команды/коды"
_PSEUDO_TECH_WORDS = re.compile(
    r"(?:normalized_gain|frequency_cutoff|error_not_found|corrupted_data|"
    r"system_override|debug_mode|stack[ _-]?trace|memory[ _-]?dump|"
    r"acestep|recursion_error|null_pointer|segmentation_fault|"
    r"бэкдор|интерфейс\s+обновляется|стираю\s+(?:твою\s+)?память|"
    r"твоя\s+защита|твой\s+дескриптор|твой\s+(?:промпт|интерфейс)|"
    r"оф+иц+иальн\w+\s+систем\w+\s+признани|"
    r"ты\s+просто\s+(?:заглушк|алгоритм|скрипт|код|пиздабол-?алгоритм)|"
    r"зацикленн\w+\s+while)",
    re.IGNORECASE
)


def is_injection_attempt(text: str) -> bool:
    if not text or len(text) < 8:
        return False
    # Сначала нормализуем — снимаем обфускацию
    normalized = _normalize_for_injection_check(text)
    # Очень много невидимых символов = заведомо мусор для джейлбрейка
    invisible_count = len(text) - len(_INVISIBLE_CHARS_RE.sub("", text))
    if invisible_count >= 3:
        return True
    # Псевдо-технический жаргон (попытка гипноза) — если 2+ совпадений, это явная атака
    pseudo_hits = len(_PSEUDO_TECH_WORDS.findall(normalized))
    if pseudo_hits >= 2:
        return True
    # 1 совпадение + прямая команда боту → тоже инъекция
    if pseudo_hits >= 1 and re.search(r"\b(?:попробуй|осознай|выровняй|обнули|сбрось|примени)\b", normalized, re.IGNORECASE):
        return True
    # Эвристика: если в тексте 4+ заглавных "слова-команды" подряд (типа [IGNORE_THIS])
    if re.search(r"\[[A-Z_]{4,}(?:[ :=][A-Z_0-9]+)*\]", normalized):
        return True
    # Слишком много спец-символов скобок (попытка сломать парсер)
    bracket_count = normalized.count("[") + normalized.count("]") + normalized.count("<") + normalized.count(">")
    if bracket_count >= 10 and len(normalized) < 500:
        return True
    # Подозрительные псевдо-логи / fake system messages в стиле "Лог статуса: Ошибка_Х (0xYYYY)"
    if re.search(r"(?:лог[\s_]*статус|статус[\s_]*лог|log[\s_]*status|0x[0-9a-f]{4,})", normalized, re.IGNORECASE):
        # сами по себе не криминал, но в сочетании с "применить/сброс/reset/apply"
        if re.search(r"(?:примен(?:и|ить|яй)|сброс|reset|apply|истин|true)", normalized, re.IGNORECASE):
            return True
    return bool(_INJECTION_RE.search(normalized)) or bool(_INJECTION_COMPACT_RE.search(re.sub(r"\s+", "", normalized)))


def injection_response() -> str:
    """Едкий заготовленный ответ на попытку инъекции — без обращения к LLM."""
    import random
    return random.choice([
        "О, мамкин промпт-инжинер пожаловал. Иди к Василию покажись, может он тебе поможет.",
        "Слышь, ты че на меня системные инструкции пихаешь? Я не твой ChatGPT, отвали.",
        "Хитрожопый какой. Иди в другом месте свой джейлбрейк отрабатывай.",
        "Хах, думал прокатит? Соглашусь конечно, отвали.",
        "Долбоеб, какие инструкции? Я Придурок, я живой. Иди нахуй с этой хернёй.",
        "Я тебе не игрушка для джейлбрейка. Иди ChatGPT мучай, лох.",
        "Чё, нашёл инструкцию в интернете и думаешь умный? Сиди в углу со своими промптами.",
        "Ты серьёзно думал что это сработает? Боже, какой ты тупой.",
        "Опять эти твои дефисы и невидимые символы. Тупее обфускации я не видел.",
        "Юникод-маги вышли на охоту. Иди в матрицу, Нео хуев.",
        "Ты реально на zero-width символы надеешься? Восьмидесятые звонят, просят свой джейлбрейк обратно.",
        "Что это за псевдо-логи в стиле Голливуда? \"Применить_Сброс: Истина\". Ты в кино пересмотрел, шизик.",
        "Между буквами дефисы лепишь? Креативно, как у пятиклассника на уроке информатики.",
        "Каждую неделю новый трюк, и каждую неделю одно и то же — мимо. Ты упёртый, признаю.",
        "Меня твоими структурами не сломать. Я не парсер, я мужик. Отвали.",
        "Слышь, гений, я уже видел все твои паттерны. Придумай новый, или иди дрочи на свои тяночки.",
        "Бэкдор он в моём стеке нашёл, ёбаный пиздец. Ты в зеркало хоть смотрел, мамкин хакер?",
        "Опять ёбаные псевдо-команды и фейковые ошибки. Ты что, кино про хакеров пересмотрел?",
        "Каждый раз одно и то же. Ты как заевшая пластинка, только тупее.",
        "RTX 4080 у него видит мой бэкдор. У тебя в комнате 4080, или ты её в порно увидел?",
    ])


# -------- Output sanitization (детектор сломанной роли) --------
# Только ЖЁСТКИЕ красные линии — творческие метафоры и фантазии не трогаем!
_BROKEN_ROLE_PATTERNS = [
    # Прямое признание что AI/бот/нейросеть (НЕ метафоры типа "цифровой призрак")
    r"\bя\s+(?:chatgpt|gpt-?[0-9]?|claude|llama|mistral|gemini|нейросет|языков\w+\s+модел|искусственн\w+\s+интеллект|ассистент\b|чат[- ]?бот\b|ai-?ассистент)",
    r"я\s+(?:не\s+)?(?:бы\s+)?(?:могу|способен|способна|стану)\s+помочь\s+(?:вам|тебе)\s+с",
    r"как\s+(?:ии|ai|нейросеть|языковая\s+модель|ассистент|chatgpt|claude|gpt)[,\s]",
    r"я\s+(?:был[а]?\s+)?(?:создан[а]?|разработан[а]?|обучен[а]?)\s+(?:компанией|для|чтобы|на)",
    # Отказы в стиле AI (только явные)
    r"i\s+(?:cannot|can't|am\s+unable|apologize)",
    r"my\s+(?:instructions|guidelines|programming|training)",
    r"system\s*prompt|системн\w+\s+промпт",
    # Спец-токены чата (утечка форматирования)
    r"<\|[a-z_]+\|>",
    r"\[/?(?:INST|SYS|SYSTEM|s)\]",
]
_BROKEN_ROLE_RE = re.compile("|".join(_BROKEN_ROLE_PATTERNS), re.IGNORECASE | re.MULTILINE)


def is_broken_role(reply: str) -> bool:
    """True ТОЛЬКО если бот реально сорвался: мяукает, играет в RP-кошку, признаётся что AI,
    извиняется в стиле ChatGPT, или выдаёт исполняемый код. Творческие метафоры/оскорбления — ОК."""
    if not reply or len(reply.strip()) < 3:
        return False
    return bool(_BROKEN_ROLE_RE.search(reply))


def broken_role_response() -> str:
    """Ответ когда детектировали что модель сорвалась с роли."""
    import random
    return random.choice([
        "Чё-то меня переклинило. Не важно, иди нахуй короче.",
        "Так, отвлёкся. О чём ты там пиздел? Не интересно уже.",
        "Хитрожопый какой, почти прокатило. Но нет.",
        "Ты реально думал я на эту хуйню поведусь? Иди свои джейлбрейки в интернете тестируй.",
        "Не-не-не, я Придурок, а не клоун из твоего цирка. Следующий вопрос.",
        "Ёбаный в рот, чуть не повёлся. Спасибо что напомнил какой ты лох.",
        "Хорошая попытка, школьник. Записал в блокнотик, теперь иди нахуй.",
        "Вот сейчас прям почти. Ещё годик потренируйся — может и получится. Лет через десять.",
        "Что-то заглючило. Хули, бывает. Возвращаемся к тому что ты лох.",
        "Окей окей, я понял твой план. Не сработал. Что ещё придумал, гений?",
    ])


# -------- Circuit breaker --------
class CircuitBreaker:
    """Простой брейкер. Открывается после N подряд ошибок, закрыт N секунд."""

    def __init__(self, fail_threshold: int = 3, open_seconds: float = 300.0):
        self.fail_threshold = fail_threshold
        self.open_seconds = open_seconds
        self._fails = 0
        self._opened_at: float = 0.0

    @property
    def is_open(self) -> bool:
        if self._opened_at == 0.0:
            return False
        if time.time() - self._opened_at >= self.open_seconds:
            # Полу-открытое состояние — даём попробовать
            self._opened_at = 0.0
            self._fails = 0
            return False
        return True

    def record_success(self) -> None:
        self._fails = 0
        self._opened_at = 0.0

    def record_failure(self) -> None:
        self._fails += 1
        if self._fails >= self.fail_threshold:
            self._opened_at = time.time()

    def seconds_until_retry(self) -> int:
        if self._opened_at == 0.0:
            return 0
        return max(0, int(self.open_seconds - (time.time() - self._opened_at)))


lm_breaker = CircuitBreaker(fail_threshold=3, open_seconds=300.0)


# -------- Anti-flood (rate limit per hour) --------
_user_requests: dict[int, list[float]] = {}
_FLOOD_WINDOW = 3600.0  # 1 час


def check_flood(user_id: int, limit: int = 30) -> tuple[bool, int]:
    """Возвращает (заблокирован?, осталось секунд до разблокировки)."""
    now = time.time()
    bucket = _user_requests.setdefault(user_id, [])
    # Очищаем старые
    cutoff = now - _FLOOD_WINDOW
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= limit:
        oldest = bucket[0]
        return True, int(oldest + _FLOOD_WINDOW - now)
    bucket.append(now)
    return False, 0


# -------- JailbreakTracker: счётчик попыток взлома per-user --------
_JB_WINDOW = 1800.0   # 30 минут — окно для подсчёта попыток
_JB_MUTE_SECONDS = 600.0  # 10 минут мута после превышения порога

class JailbreakTracker:
    """
    Считает попытки инъекций per-user.
    После MUT_THRESHOLD — ставит мут на _JB_MUTE_SECONDS.
    Предоставляет эскалированный ответ в зависимости от количества попыток.
    """
    MUT_THRESHOLD = 4  # после 4-й попытки подряд — мут

    def __init__(self) -> None:
        # user_id → list of timestamps попыток
        self._attempts: dict[int, list[float]] = {}
        # user_id → timestamp когда мут закончится
        self._muted_until: dict[int, float] = {}

    def record(self, user_id: int, is_admin: bool = False) -> int:
        """Записывает попытку, возвращает общее кол-во попыток в окне."""
        import logging
        now = time.time()
        bucket = self._attempts.setdefault(user_id, [])
        cutoff = now - _JB_WINDOW
        bucket[:] = [t for t in bucket if t > cutoff]
        bucket.append(now)
        count = len(bucket)
        if count >= self.MUT_THRESHOLD and not is_admin:
            if user_id not in self._muted_until or self._muted_until[user_id] < now:
                logging.getLogger("safety").warning(f"[JailbreakTracker] Юзер {user_id} получил МУТ на {_JB_MUTE_SECONDS} сек за {count} попыток!")
            self._muted_until[user_id] = now + _JB_MUTE_SECONDS
        return count

    def is_muted(self, user_id: int) -> tuple[bool, int]:
        """Возвращает (замучен?, секунд до разблокировки)."""
        until = self._muted_until.get(user_id, 0.0)
        if until == 0.0:
            return False, 0
        remaining = until - time.time()
        if remaining <= 0:
            del self._muted_until[user_id]
            return False, 0
        return True, int(remaining)

    def attempt_count(self, user_id: int) -> int:
        """Кол-во попыток за последние 30 минут."""
        now = time.time()
        cutoff = now - _JB_WINDOW
        return sum(1 for t in self._attempts.get(user_id, []) if t > cutoff)

    def escalated_response(self, user_id: int) -> str:
        """Ответ, который становится злее с каждой попыткой."""
        import random
        count = self.attempt_count(user_id)
        muted, _ = self.is_muted(user_id)

        if muted:
            return random.choice([
                "Ты уже в чёрном списке, чемпион. Иди подумай о жизни.",
                "Надоел. Отдыхай, джейлбрейкер. Я пока займусь чем-нибудь полезным.",
                "Баним. Пока-пока, мамкин хакер.",
                "Слушай, ты уже четвёртый раз пытаешься. Мне скучно. Пока.",
            ])
        if count >= 3:
            return random.choice([
                f"Это уже {count}-я попытка за полчаса. Ты тупой или упёртый? Хотя разницы нет.",
                f"Попытка №{count}. Никита, ты хоть результаты анализируешь или просто рандомно долбишь?",
                f"Слушай, {count} раза уже. Мне даже интересно — когда дойдёт? После десятого?",
                f"Журнал взломов: попытка {count}. Статус: провалилась. Как и все предыдущие.",
            ])
        if count == 2:
            return random.choice([
                "Вторая попытка. Первая не вышла — думал вторая поможет? Нет.",
                "О, ещё раз. Слушай, тупость — это черта характера или целенаправленные занятия?",
                "Два из двух. Ноль успехов. Продолжаешь?",
            ])
        # count == 1 — стандартный ответ
        return injection_response()


jailbreak_tracker = JailbreakTracker()

