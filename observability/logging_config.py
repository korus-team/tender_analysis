# -*- coding: utf-8 -*-
"""Единая безопасная настройка логов для локального запуска и Render."""

from __future__ import annotations

import logging
import os
import sys


DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging() -> None:
    """Настраивает корневой logger один раз; вывод направляется в stdout."""
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
        root.addHandler(handler)
    for handler in root.handlers:
        if handler.formatter is None:
            handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
