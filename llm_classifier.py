# -*- coding: utf-8 -*-
"""
ОПЦИОНАЛЬНЫЙ ИИ-слой оценки тендера (шаг 3, второй уровень).

Когда включать:
  Правила из scoring.py дают дешёвую, локальную и объяснимую оценку и покрывают
  большинство случаев. LLM подключают ВТОРЫМ слоем — для пограничных тендеров
  (например, балл правил 40..70), где нужно "понять смысл" сложного описания.

ВАЖНО про приватность (ТЗ, раздел 20.10):
  Этот модуль отправляет текст тендера во внешний сервис (Anthropic API).
  С заказчиком нужно согласовать, допустимо ли это. Если данные чувствительны —
  используйте только слой правил или локальную (self-hosted) модель. Поэтому
  по умолчанию LLM ВЫКЛЮЧЕН (см. main.py, USE_LLM = False).

Как включить:
  1) pip install anthropic
  2) задать ключ:  переменная окружения ANTHROPIC_API_KEY
  3) в main.py поставить USE_LLM = True

Документация API: https://docs.claude.com/en/api/overview
"""

from __future__ import annotations
import json
import os

# Быстрая и недорогая модель — разумный выбор для классификации.
# Список актуальных моделей — в документации выше.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def _build_prompt(tender: dict, icp: dict) -> str:
    return (
        "Ты помогаешь тендерному отделу решить, подходит ли закупка компании.\n\n"
        f"ПРОФИЛЬ КОМПАНИИ (что нам интересно): {icp['name']}.\n"
        f"Профильные темы: {', '.join(icp.get('keywords_any', []))}.\n"
        f"Стоп-темы (точно не наше): {', '.join(icp.get('stop_words', []))}.\n"
        f"Комфортный бюджет: {icp.get('budget_min')}–{icp.get('budget_max')} руб.\n\n"
        "ТЕНДЕР:\n"
        f"- Предмет: {tender.get('subject')}\n"
        f"- Категория: {tender.get('category')}\n"
        f"- Заказчик: {tender.get('customer')}\n"
        f"- Регион: {tender.get('region')}\n"
        f"- Начальная цена, руб.: {tender.get('price_rub')}\n"
        f"- Дней до дедлайна: {tender.get('days_left')}\n\n"
        "Оцени релевантность и верни СТРОГО JSON без пояснений вокруг, вида:\n"
        '{"fit": true/false, "score": 0-100, "explanation": "1-2 предложения почему"}'
    )


def classify_with_llm(tender: dict, icp: dict, model: str = DEFAULT_MODEL) -> dict:
    """
    Возвращает {"fit": bool, "score": int, "explanation": str}.
    Бросает понятную ошибку, если не установлен SDK или нет ключа.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("Не установлен пакет anthropic. Выполните: pip install anthropic") from exc

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("Не задан ANTHROPIC_API_KEY (переменная окружения).")

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=300,
        system="Ты — строгий классификатор тендеров. Отвечай только валидным JSON.",
        messages=[{"role": "user", "content": _build_prompt(tender, icp)}],
    )

    raw = "".join(block.text for block in resp.content if getattr(block, "type", None) == "text")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"fit": None, "score": None, "explanation": f"Не удалось разобрать ответ модели: {raw[:200]}"}

    return {
        "fit": data.get("fit"),
        "score": data.get("score"),
        "explanation": data.get("explanation", ""),
    }