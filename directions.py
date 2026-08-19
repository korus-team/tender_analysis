# -*- coding: utf-8 -*-
"""Предметная классификация тендеров для отдела DAR.

Правила здесь намеренно контекстные: одиночные части слов не считаются
доказательством релевантности. Это защищает, например, от ``PML-300`` как ML,
``внесения изменений в лицензию на недра`` как лицензии ПО и обычной
``отчётности`` без связи с ИТ/аналитикой.
"""
from __future__ import annotations

import re


# key, отображаемое имя, иконка (id из SVG-спрайта).
DIRECTIONS = [
    ("license", "Лицензии", "i-tasks", []),
    ("dwh", "Данные / ДВХ", "i-db", []),
    ("bi", "Дашборды / BI", "i-chart", []),
    ("bigdata", "Big Data", "i-bars", []),
    ("ml", "Аналитика / ML", "i-ai", []),
    ("datagov", "Управление данными", "i-grid", []),
    ("software", "Разработка / ИТ-системы", "i-ai", []),
]

_OTHER = ("other", "Другое", "i-grid")
_META = {key: (name, icon) for key, name, icon, _ in DIRECTIONS}
_META[_OTHER[0]] = (_OTHER[1], _OTHER[2])


def _rx(*patterns: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(pattern, re.I) for pattern in patterns)


_LICENSE = _rx(
    r"\bлицензи(?:я|и|й|ю|онн\w*)\b",
    r"\bнеисключительн\w+\s+прав\w*\b",
    r"\bправ\w*\s+(?:на\s+)?использован\w+\s+(?:программ|по\b|субд\b)",
    r"\bпродлени\w*\s+(?:подписк|прав\w*\s+использован)",
    r"\bлицензируем\w*.{0,35}(?:программ|по\b)",
    r"(?:сертификат|подписк)\w*.{0,55}техническ\w*\s+поддержк\w*.{0,35}(?:программ|субд|по\b)",
    r"техническ\w*\s+поддержк\w*.{0,35}(?:программ\w*\s+продукт|субд\b).{0,35}(?:сертификат|подписк)",
)
_NON_SOFTWARE_LICENSE = _rx(
    r"лицензи\w*.{0,45}\b(?:недр|водопользован|скважин|геологическ)\w*",
    r"лицензи\w*.{0,45}\b(?:медицинск|образовательн|строительн|охранн)\w*\s+деятельност",
    r"\b(?:недр|водопользован|скважин)\w*.{0,45}лицензи\w*",
)

_RULES: list[tuple[str, tuple[re.Pattern, ...]]] = [
    ("dwh", _rx(
        r"\b(?:дхд|dwh)\b", r"\bdata\s+(?:warehouse|lake)\b",
        r"хранилищ\w*\s+данн", r"витрин\w*\s+данн", r"озер\w*\s+данн",
        r"\b(?:greenplum|vertica|clickhouse|teradata)\b",
        r"\betl\b", r"интеграц\w*\s+данн", r"консолидац\w*\s+данн",
    )),
    ("bi", _rx(
        r"\bbi\b", r"\b(?:power\s*bi|qlik|tableau|superset|datalens)\b",
        r"\bdashboard\w*\b", r"дашборд\w*", r"аналитическ\w*\s+панел\w*",
        r"визуализац\w*\s+данн",
        r"разработк\w*.{0,35}отч[её]т\w*.{0,45}(?:данн|хранилищ)",
        r"(?:систем|автоматизац|консолидац)\w*.{0,45}отч[её]тност",
        r"отч[её]тност\w*.{0,45}(?:систем|данн|аналит)",
    )),
    ("bigdata", _rx(
        r"\bbig\s+data\b", r"больш\w*\s+данн", r"\b(?:hadoop|spark|kafka)\b",
        r"потоков\w*\s+обработк", r"стриминг\w*", r"\bdata\s+engineer\b",
        r"инженер\w*\s+данн", r"конвейер\w*\s+данн",
    )),
    ("ml", _rx(
        r"машинн\w*\s+обучен", r"искусственн\w*\s+интеллект",
        r"\bmachine\s+learning\b", r"\bdata\s+science\b", r"нейросет\w*",
        r"\bml[-\s]+(?:решен|модел|платформ|систем|алгоритм)",
        r"\bai[-\s]+(?:development|platform|решен|сервис|систем|станц)",
        r"предиктивн\w*", r"прогнозн\w*\s+модел", r"генеративн\w*",
        r"распознаван\w*", r"обработк\w*\s+естественн\w*\s+язык",
    )),
    ("datagov", _rx(
        r"\bнси\b", r"\bmdm\b", r"мастер[-\s]?данн", r"качеств\w*\s+данн",
        r"\bdata\s+governance\b", r"метаданн\w*", r"каталог\w*\s+данн",
        r"управлен\w*\s+данн", r"нормативн\w*[-\s]+справочн",
    )),
    ("software", _rx(
        r"разработк\w*.{0,60}(?:программ\w*|информационн\w*\s+систем|"
        r"интернет[-\s]?портал|(?:веб|онлайн)[-\s]?(?:сайт|портал)|платформ|цифров\w*.{0,30}модел)",
        r"(?:программ\w*|информационн\w*\s+систем|платформ).{0,60}разработк\w*",
        r"(?:доработк|модификац|развити)\w*.{0,55}(?:программ\w*|\bпо\b|ит[-\s]?систем|"
        r"информационн\w*\s+систем|платформ|портал|приложен)",
        r"внедрен\w*.{0,55}(?:программ\w*|информационн\w*\s+систем|ит[-\s]?систем|"
        r"платформ|\berp\b|\bcrm\b|\bwms\b|\bbpm\w*)",
        r"сопровождени\w*.{0,55}(?:программ\w*|\bпо\b|информационн\w*\s+систем|ит[-\s]?систем)",
        r"автоматизац\w*.{0,45}(?:бизнес[-\s]?процесс|разработк|тестирован|документирован)",
        r"развити\w*.{0,30}функционал\w*.{0,25}систем",
        r"(?:доработк|развити)\w*.{0,35}систем\w*\s+(?:абс|1с)\b",
        r"(?:модификац|сопровождени)\w*.{0,45}\bисс\b",
        r"разработк\w*.{0,35}аналитическ\w*.{0,25}инструмент",
        r"\b(?:erp|crm|wms|bpmsoft)\b",
        r"\b1с\s*[:\-]",
        r"\b(?:интернет[-\s]?портал|(?:веб|онлайн)[-\s]?(?:сайт|портал)|мобильн\w*\s+приложен)\b",
    )),
]


def _haystack(tender: dict) -> str:
    parts = [tender.get("subject"), tender.get("title"), tender.get("category")]
    text = " ".join(str(part) for part in parts if part)
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def classify(tender: dict) -> str:
    """Возвращает предметный тег тендера, учитывая контекст фразы."""
    text = _haystack(tender)
    if any(pattern.search(text) for pattern in _LICENSE):
        if not any(pattern.search(text) for pattern in _NON_SOFTWARE_LICENSE):
            return "license"

    for key, patterns in _RULES:
        if any(pattern.search(text) for pattern in patterns):
            return key
    return _OTHER[0]


def is_relevant(tender: dict) -> bool:
    """Подходит ли предмет закупки профилю DAR."""
    return classify(tender) != _OTHER[0]


def name_of(key: str) -> str:
    return _META.get(key, (_OTHER[1], _OTHER[2]))[0]


def icon_of(key: str) -> str:
    return _META.get(key, (_OTHER[1], _OTHER[2]))[1]


def all_keys(include_other: bool = True) -> list[str]:
    keys = [key for key, *_ in DIRECTIONS]
    if include_other:
        keys.append(_OTHER[0])
    return keys
