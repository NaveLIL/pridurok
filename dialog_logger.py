"""Логирование диалогов в файл."""
import logging
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


def log_dialog(user: str, channel: str, prompt: str, reply: str) -> None:
    dialog.info(
        "USER=%s CH=%s\n  >> %s\n  << %s",
        user,
        channel,
        prompt.replace("\n", " "),
        reply.replace("\n", " "),
    )
