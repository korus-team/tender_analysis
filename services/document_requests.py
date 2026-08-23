# -*- coding: utf-8 -*-
"""Формирование текста запроса тендерной документации."""

from __future__ import annotations


def build_document_request(tender: dict) -> str:
    """Возвращает готовый к копированию текст с названием и лучшей ссылкой."""
    details = tender.get("details") if isinstance(tender.get("details"), dict) else {}
    link = (details.get("etp_url") or details.get("kontur_url") or
            tender.get("url") or "").strip()
    title = (tender.get("title") or "закупка без названия").strip()
    link_part = f" ({link})" if link else ""
    return (
        f"Добрый день, коллеги! Просьба скачать документацию по закупке "
        f"«{title}»{link_part} и переслать нам. Спасибо!"
    )
