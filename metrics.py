"""Метрики работы бота: счётчики, латентности, аптайм."""
import time
from collections import deque, defaultdict

_started_at = time.time()
_replies_total = 0
_errors_total = 0
_blurts_total = 0
_pings_total = 0
_reactions_total = 0
_latencies: deque[float] = deque(maxlen=100)  # секунды на ответ
_per_user_replies: dict[int, int] = defaultdict(int)
_tokens_per_sec: deque[float] = deque(maxlen=20)


def started_at() -> float:
    return _started_at


def uptime_str() -> str:
    sec = int(time.time() - _started_at)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}д {h}ч {m}м"
    return f"{h}ч {m}м {s}с"


def record_reply(user_id: int, latency: float, tokens: int = 0) -> None:
    global _replies_total
    _replies_total += 1
    _latencies.append(latency)
    _per_user_replies[user_id] += 1
    if tokens > 0 and latency > 0:
        _tokens_per_sec.append(tokens / latency)


def record_error() -> None:
    global _errors_total
    _errors_total += 1


def record_blurt() -> None:
    global _blurts_total
    _blurts_total += 1


def record_ping() -> None:
    global _pings_total
    _pings_total += 1


def record_reaction() -> None:
    global _reactions_total
    _reactions_total += 1


def avg_latency() -> float:
    return sum(_latencies) / len(_latencies) if _latencies else 0.0


def avg_tokens_per_sec() -> float:
    return sum(_tokens_per_sec) / len(_tokens_per_sec) if _tokens_per_sec else 0.0


def top_users(n: int = 5) -> list[tuple[int, int]]:
    return sorted(_per_user_replies.items(), key=lambda x: x[1], reverse=True)[:n]


def snapshot() -> dict:
    return {
        "uptime": uptime_str(),
        "replies": _replies_total,
        "errors": _errors_total,
        "blurts": _blurts_total,
        "pings": _pings_total,
        "reactions": _reactions_total,
        "avg_latency_s": round(avg_latency(), 2),
        "avg_tok_s": round(avg_tokens_per_sec(), 1),
        "unique_users": len(_per_user_replies),
    }
