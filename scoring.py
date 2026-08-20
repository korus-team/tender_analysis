# -*- coding: utf-8 -*-
"""
Скоринг релевантности тендера профилю ICP (шаг 3, слой правил).

Прозрачный и объяснимый: возвращает балл 0..100, вердикт, список причин и меток.
Именно объяснимость требует ТЗ (раздел 10.4) — пользователь должен видеть, ПОЧЕМУ
тендер оценён так. Это первый (дешёвый, локальный, без ключей) слой оценки.
Второй слой — LLM — опционален (см. llm_classifier.py).
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field

import directions

LLM_WEIGHT = 0.8
RULES_WEIGHT = 0.2


def _contains(text: str, term: str) -> bool:
    """
    Совпадение по границе слова: 'система' не сматчит 'подсистема',
    а предлог 'по' не мог бы сматчить 'поставка'. При этом стемы
    сохраняются: 'информацион' по-прежнему ловит 'информационных'
    (границу требуем только в начале слова, суффиксы разрешены).
    """
    term = term.strip().lower()
    if not term:
        return False
    return re.search(r"\b" + re.escape(term), text) is not None


@dataclass
class ScoreResult:
    score: int                              # 0..100
    verdict: str                            # take | review | low | reject
    reasons: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


def _haystack(tender: dict) -> str:
    """Текст, по которому ищем ключевые слова: предмет + заголовок + категория."""
    parts = [tender.get("subject"), tender.get("title"), tender.get("category")]
    return " ".join(p for p in parts if p).lower()


def theme_score(tender: dict, query: str) -> tuple[int, list[str]]:
    """
    Прозрачный балл по СВОБОДНОМУ запросу пользователя (поле «тема»).
    Балл = доля слов запроса, найденных в тексте тендера, с грубым учётом окончаний.
    Возвращает (балл 0..100, список совпавших слов запроса) — чтобы было видно, ПОЧЕМУ.
    """
    text = _haystack(tender)
    tokens = [w for w in re.split(r"[^\w]+", query.lower()) if len(w) >= 3]
    if not tokens:
        return 0, []
    matched = []
    for tok in tokens:
        stem = tok[:-2] if len(tok) >= 6 else tok    # грубый стем: убираем окончание
        if stem and stem in text:
            matched.append(tok)
    score = round(100 * len(matched) / len(tokens))
    return score, matched


def score_tender(tender: dict, icp: dict) -> ScoreResult:
    text = _haystack(tender)
    reasons: list[str] = []
    labels: list[str] = []
    direction = directions.classify(tender)
    is_license = direction == "license"

    # ---------------- Жёсткие стоп-факторы -> сразу reject ---------------- #
    for stop in icp.get("stop_words", []):
        if _contains(text, stop):
            return ScoreResult(0, "reject", [f"Стоп-слово: «{stop}»"], ["стоп-слово"])

    region = (tender.get("region") or "")
    for ex in icp.get("regions_exclude", []):
        if ex.lower() in region.lower():
            return ScoreResult(0, "reject", [f"Исключённый регион: {ex}"], ["регион исключён"])

    days_left = tender.get("days_left")
    if days_left is not None and days_left < 0:
        return ScoreResult(
            0, "reject",
            ["Срок подачи заявок уже истёк"],
            ["дедлайн прошёл"],
        )
    if days_left is not None and days_left <= 3 and not is_license:
        return ScoreResult(
            0, "reject",
            [f"До дедлайна {days_left} дн. — такие закупки оставляем только для лицензий"],
            ["дедлайн близко"],
        )

    price = tender.get("price_rub")
    bmin, bmax = icp.get("budget_min"), icp.get("budget_max")
    budget_too_high = bool(price and bmax and price > bmax)
    budget_too_low = bool(price and bmin and price < bmin)

    weights = icp["weights"]
    score = 0.0

    # Предметное соответствие — главный фактор после порога по обороту.
    # Конкретный тег одновременно даёт понятное объяснение пользователю.
    if direction != "other":
        score += 40
        reasons.append(f"Предмет закупки относится к направлению «{directions.name_of(direction)}»")
        labels.append("профильное направление")

    # Лицензии — отдельный приоритет заказчика. Короткий срок для них допустим.
    if is_license:
        score += 20
        reasons.append("Лицензионная закупка — приоритетное и маржинальное направление")
        labels.append("лицензия")

    # ---------------- Ключевые слова (главный фактор) ---------------- #
    # Вклад делится на две части, чтобы движок РАЗЛИЧАЛ уровень работы:
    #   базовая (до 45% веса) — за попадание в профильные темы вообще;
    #   усиление (до 55% веса) — за высокоценную работу (разработка/внедрение/ИИ...).
    # Так реальный проект обгоняет "просто тему", а перепродажа остаётся внизу.
    kw_w = weights["keywords"]
    any_hits = [k for k in icp.get("keywords_any", []) if _contains(text, k)]
    if any_hits:
        coverage = min(1.0, len(any_hits) / 3)          # 3+ темы = максимум базовой части
        base = kw_w * 0.45 * coverage
        boost = sum(pts for word, pts in icp.get("keywords_boost", {}).items() if _contains(text, word))
        boost_points = min(kw_w * 0.55, boost)
        score += base + boost_points
        reasons.append(f"Совпадение по темам: {', '.join(any_hits[:4])}")
        labels.append("профильная тема")
    else:
        reasons.append("Нет совпадений по ключевым темам профиля")
        labels.append("тема не наша")

    # ---------------- Штраф за непрофиль (перепродажа/поставка) ---------------- #
    penalty_hits = [w for w in icp.get("keywords_penalty", {}) if _contains(text, w)]
    if is_license:
        # Для лицензионного направления поставка, продление, название продукта
        # со словом Monitor и передача прав — нормальный предмет закупки.
        penalty_hits = []
    if penalty_hits:
        penalty = sum(icp["keywords_penalty"][w] for w in penalty_hits)
        score -= penalty
        reasons.append(f"Понижение (похоже на перепродажу/поставку): {', '.join(penalty_hits[:3])}")
        labels.append("не профиль: поставка/лицензии")

    # ---------------- Регион ---------------- #
    if any(pref.lower() in region.lower() for pref in icp.get("regions_preferred", [])):
        score += weights["region"]
        reasons.append(f"Приоритетный регион: {region}")
        labels.append("приоритетный регион")

    # ---------------- Бюджет ---------------- #
    # В рабочем диапазоне — полный балл (диапазон широкий: и сотни тысяч, и десятки млн
    # одинаково подходят). Вне диапазона тендер уже отклонён жёстким фильтром выше.
    if price is None or price == 0:
        reasons.append("Начальная цена не указана — проверить вручную")
        labels.append("цена не указана")
    elif budget_too_high:
        reasons.append(f"Бюджет {price:,} руб. выше настроенного диапазона — это не стоп-фактор")
        labels.append("бюджет велик")
    elif budget_too_low:
        reasons.append(f"Бюджет {price:,} руб. ниже настроенного диапазона — это не стоп-фактор")
        labels.append("бюджет мал")
    elif bmin and bmax:
        score += weights["budget"]
        reasons.append(f"Бюджет {price:,} руб. — в рабочем диапазоне")
        labels.append("бюджет ок")

    # ---------------- Дедлайн ---------------- #
    if days_left is not None:
        if days_left >= 14:
            score += weights["deadline"]
            reasons.append(f"До дедлайна {days_left} дн. — времени достаточно")
        elif is_license and days_left <= 3:
            reasons.append(f"До дедлайна {days_left} дн., но для лицензии короткий срок допустим")
            labels.append("срочная лицензия")
        elif days_left > 3:
            score += weights["deadline"] * 0.5
            reasons.append(f"До дедлайна {days_left} дн. — время ограничено")
            labels.append("дедлайн близко")

    score_int = max(0, min(100, round(score)))

    if score_int >= 70:
        verdict = "take"
    elif score_int >= 40:
        verdict = "review"
    else:
        verdict = "low"

    return ScoreResult(score_int, verdict, reasons, labels)


def score_tender_llm(tender: dict, icp: dict, *, scorer=None) -> ScoreResult:
    """Return the hybrid score: 80% LLM semantic score and 20% rule score.

    The rule result remains useful as a transparent audit trail: its labels and
    reasons are retained alongside the LLM's direction, uncertainty and reason.
    Pass one shared ``OpenAITenderScorer`` instance from the caller so a batch
    does not recreate the API client for every tender.
    Возвращает следующую оценку тендера LLM-кой:
    1. score (0...100)
    2. verdict
    3. reasons
    4. labels
    """
    rules_result = score_tender(tender, icp)

    if scorer is None:
        from LLM_scoring import OpenAITenderScorer

        scorer = OpenAITenderScorer()

    llm_result = scorer.score(_llm_input(tender))
    evaluation = llm_result.evaluation
    score = round(LLM_WEIGHT * evaluation.score + RULES_WEIGHT * rules_result.score)

    if score >= 70:
        verdict = "take"
    elif score >= 40:
        verdict = "review"
    else:
        verdict = "low"

    return ScoreResult(
        score=score,
        verdict=verdict,
        reasons=[*rules_result.reasons, f"LLM: {evaluation.reason}"],
        labels=[
            *rules_result.labels,
            "llm-scored",
            f"llm-direction:{evaluation.direction.value}",
            f"uncertainty:{evaluation.uncertainty.value}",
        ],
    )


def _llm_input(tender: dict) -> str:
    """ИНформация передаваемая LLM"""
    fields = (
        ("Предмет", tender.get("subject") or tender.get("title")),
        ("Заказчик", tender.get("customer")),
    )
    # ``OpenAITenderScorer`` accepts at most 2,000 characters per request.
    return "\n".join(f"{name}: {value}" for name, value in fields if value)[:2_000]
