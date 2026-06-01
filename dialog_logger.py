"""Логирование диалогов в файл."""
import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _make_logger(name: str, filename: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    handler = RotatingFileHandler(
        LOG_DIR / filename,
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


dialog = _make_logger("pridurok.dialog", "dialog.log")
system = _make_logger("pridurok.system", "system.log")
analysis_log = LOG_DIR / "dialog_events.jsonl"


def _append_analysis_record(record: dict[str, object]) -> None:
    with analysis_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_dialog(user: str, channel: str, prompt: str, reply: str, source: str = "message") -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    dialog.info(
        "USER=%s CH=%s\n  >> %s\n  << %s",
        user,
        channel,
        prompt.replace("\n", " "),
        reply.replace("\n", " "),
    )
    _append_analysis_record(
        {
            "ts": timestamp,
            "source": source,
            "user": user,
            "channel": channel,
            "prompt": prompt,
            "reply": reply,
        }
    )
