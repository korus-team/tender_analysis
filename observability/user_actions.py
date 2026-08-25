# -*- coding: utf-8 -*-
"""Отдельный ротируемый журнал значимых действий пользователей."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_PATH = Path("logs/user_actions.log")
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def configure_user_action_logging() -> logging.Logger:
    """Вернуть logger, который пишет только пользовательские действия в файл."""
    logger = logging.getLogger("user_actions")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    configured_path = Path(os.getenv("USER_ACTION_LOG_FILE", str(DEFAULT_LOG_PATH)))
    log_path = configured_path if configured_path.is_absolute() else PROJECT_ROOT / configured_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        log_path,
        maxBytes=DEFAULT_MAX_BYTES,
        backupCount=DEFAULT_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    return logger
