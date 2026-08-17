# -*- coding: utf-8 -*-
"""
Направления департамента DAR (КОРУС Консалтинг) — аналитические решения.

Классифицируем тендер по тексту (тема/название) в одно из направлений.
Порядок в списке = приоритет: проверяем сверху вниз, берём первое совпадение.
Лицензии стоят первыми — они особенно интересны и не должны теряться,
даже если в тексте есть слова про BI/ДХД.
"""
from __future__ import annotations
import re

# key, отображаемое имя, иконка (id из спрайта), ключевые слова (по основам)
DIRECTIONS = [
    ("license", "Лицензии", "i-tasks", [
        "лицензи", "неисключительн", "право использ", "права использ",
        "продлени подписк", "продлени прав", "сублицензи", "поставк по",
        "поставка программн", "право на использ", "лицензионн",
    ]),
    ("dwh", "Данные / ДВХ", "i-db", [
        "хранилищ данных", "хранилища данных", "хранилище данных", "дхд", "dwh",
        "витрин данных", "витрина данных", "data warehouse", "data lake",
        "озеро данных", "greenplum", "vertica", "clickhouse", "teradata",
        "etl", "интеграц данн", "консолидац данн",
    ]),
    ("bi", "Дашборды / BI", "i-chart", [
        "дашборд", "dashboard", "power bi", "qlik", "tableau", "superset",
        "datalens", "визуализац данн", "отчётност", "отчетност",
        "аналитическ отчёт", "аналитическ панел", "бизнес-аналитик", " bi ",
        "система отчётности", "витрина отчёт",
    ]),
    ("bigdata", "Big Data", "i-bars", [
        "big data", "большие данные", "hadoop", "spark", "kafka",
        "потоков обработк", "стриминг", "data engineer", "инженер данных",
        "конвейер данн", "обработк больших",
    ]),
    ("ml", "Аналитика / ML", "i-ai", [
        "машинн обучен", "искусственн интеллект", "предиктивн", "data science",
        "нейросет", "прогнозн модел", "генеративн", "ml-", "recommend",
        "распознаван", "обработк естественн язык",
    ]),
    ("datagov", "Управление данными", "i-grid", [
        "нси", "мастер-данн", "mdm", "качеств данн", "data governance",
        "метаданн", "каталог данн", "управлен данными", "нормативно-справочн",
    ]),
]

_OTHER = ("other", "Другое", "i-grid")

_COMPILED = [(key, name, icon, [k.lower() for k in kws]) for key, name, icon, kws in DIRECTIONS]
_META = {key: (name, icon) for key, name, icon, _ in _COMPILED}
_META[_OTHER[0]] = (_OTHER[1], _OTHER[2])


def _haystack(tender: dict) -> str:
    parts = [tender.get("subject"), tender.get("title"), tender.get("category")]
    return " " + " ".join(p for p in parts if p).lower() + " "


def classify(tender: dict) -> str:
    """Возвращает ключ направления DAR для тендера."""
    text = _haystack(tender)
    for key, _name, _icon, kws in _COMPILED:
        if any(k in text for k in kws):
            return key
    return _OTHER[0]


def is_relevant(tender: dict) -> bool:
    """Профильный ли это для DAR тендер (попал в одно из направлений, не «Другое»)."""
    return classify(tender) != _OTHER[0]


def name_of(key: str) -> str:
    return _META.get(key, (_OTHER[1], _OTHER[2]))[0]


def icon_of(key: str) -> str:
    return _META.get(key, (_OTHER[1], _OTHER[2]))[1]


def all_keys(include_other: bool = True) -> list[str]:
    keys = [k for k, *_ in _COMPILED]
    if include_other:
        keys.append(_OTHER[0])
    return keys