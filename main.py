# -*- coding: utf-8 -*-
"""
Оркестратор сбора тендеров (одна кнопка: собрать + обогатить).

run_ingest():
  1) собирает тендеры из всех включённых источников (sources.collect_all)
  2) убирает дубли между источниками
  3) ГЛАВНЫЙ фильтр — размер компании-заказчика >= 10 млрд ₽
     (company_size.passes_revenue); не прошедшие даже не добавляются
  4) считает релевантность (scoring.score_tender) и сохраняет
  5) сразу обогащает добавленные тендеры (enrich.enrich_pending)

rescore_all() — пересчёт баллов у всех тендеров (используется профилем ICP).
"""
from __future__ import annotations
from datetime import datetime

import storage
import sources
import company_size
import directions
from scoring import score_tender, score_tender_llm
from icp_config import load_icp

try:
    from enrich import enrich_pending
except ImportError:  # обогащение опционально
    enrich_pending = None

ENRICH_MIN_SCORE = 50


def _days_left(deadline: str | None):
    if not deadline:
        return None
    try:
        return (datetime.fromisoformat(deadline) - datetime.now()).days
    except (ValueError, TypeError):
        return None


def run_ingest(max_pages: int = 50, use_llm: bool = False, write_json: bool = True) -> dict:
    icp = load_icp()
    conn = storage.connect()
    llm_scorer = None
    if use_llm:
        from LLM_scoring import OpenAITenderScorer

        llm_scorer = OpenAITenderScorer()

    # чистим просроченные (дедлайн уже прошёл) — они больше не актуальны
    now_iso = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "DELETE FROM tenders WHERE deadline IS NOT NULL AND deadline < ?", (now_iso,))
    expired_removed = cur.rowcount or 0
    # убираем осиротевшие отметки избранного на удалённые тендеры
    try:
        conn.execute("DELETE FROM user_favorites "
                     "WHERE tender_id NOT IN (SELECT tender_id FROM tenders)")
    except Exception:  # noqa: BLE001
        pass
    conn.commit()

    # инкрементально: передаём уже известные id, чтобы не перебирать всё заново
    try:
        known = storage.existing_ids(conn)
    except AttributeError:
        known = None
    collected = sources.collect_all(max_pages, known_ids=known)
    items = collected["items"]

    kept, skipped_small, relevant_n = [], 0, 0
    for t in items:
        # Единственный жёсткий фильтр сбора — размер компании-заказчика.
        # Нерелевантные НЕ выбрасываем: они нужны во вкладке «Нерелевантные» (QA).
        if not company_size.passes_revenue(t.get("customer")):
            skipped_small += 1
            continue
        t = dict(t)
        t["days_left"] = _days_left(t.get("deadline"))
        res = (
            score_tender_llm(t, icp, scorer=llm_scorer)
            if llm_scorer is not None
            else score_tender(t, icp)
        )
        t["score"] = res.score
        t["verdict"] = res.verdict
        t["reasons"] = res.reasons
        t["labels"] = res.labels
        if directions.is_relevant(t):
            relevant_n += 1
        kept.append(t)

    save_summary = storage.save_scored(conn, kept) if kept else {"new": [], "updated": []}

    # обогащение сразу, тем же проходом
    enrich_summary = {}
    if enrich_pending is not None:
        try:
            pending = storage.count_to_enrich(conn, ENRICH_MIN_SCORE)
            enrich_summary = enrich_pending(conn, limit=max(pending, 1),
                                            min_score=ENRICH_MIN_SCORE) or {}
        except Exception as e:  # noqa: BLE001
            enrich_summary = {"error": str(e)}

    conn.close()

    new = save_summary.get("new", [])
    updated = save_summary.get("updated", [])
    return {
        "per_source": collected["per_source"],
        "collected": collected["collected"],
        "after_dedupe": collected["after_dedupe"],
        "expired_removed": expired_removed,
        "skipped_small_company": skipped_small,
        "relevant": relevant_n,
        "kept": len(kept),
        "new": len(new) if isinstance(new, list) else new,
        "updated": len(updated) if isinstance(updated, list) else updated,
        "enriched": enrich_summary.get("done") if isinstance(enrich_summary, dict) else None,
    }


def rescore_all(icp: dict | None = None, use_llm: bool = False) -> int:
    icp = icp or load_icp()
    conn = storage.connect()
    llm_scorer = None
    if use_llm:
        from LLM_scoring import OpenAITenderScorer

        llm_scorer = OpenAITenderScorer()
    rows = storage.query_tenders(conn, limit=None)
    n = 0
    for t in rows:
        t = dict(t)
        t["days_left"] = _days_left(t.get("deadline"))
        res = (
            score_tender_llm(t, icp, scorer=llm_scorer)
            if llm_scorer is not None
            else score_tender(t, icp)
        )
        storage.update_score(conn, t["tender_id"], res.score, res.verdict,
                             res.reasons, res.labels)
        n += 1
    conn.commit()
    conn.close()
    return n