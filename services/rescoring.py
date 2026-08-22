"""Recalculate tender scores after the company profile changes."""

from __future__ import annotations

import logging
from datetime import datetime

import storage
from icp_config import load_icp
from scoring import score_tender, score_tender_llm

logger = logging.getLogger(__name__)


def _days_left(deadline: str | None) -> int | None:
    if not deadline:
        return None
    try:
        return (datetime.fromisoformat(deadline) - datetime.now()).days
    except (ValueError, TypeError):
        return None


def rescore_all(icp: dict | None = None, use_llm: bool = False) -> int:
    """Recalculate and persist scores for every stored tender."""
    profile = icp or load_icp()
    llm_scorer = None
    if use_llm:
        from LLM_scoring import OpenAITenderScorer

        llm_scorer = OpenAITenderScorer()

    conn = storage.connect()
    try:
        rows = storage.query_tenders(conn, limit=None)
        count = 0
        for row in rows:
            tender = dict(row)
            tender["days_left"] = _days_left(tender.get("deadline"))
            result = (
                score_tender_llm(tender, profile, scorer=llm_scorer)
                if llm_scorer is not None
                else score_tender(tender, profile)
            )
            storage.update_score(
                conn,
                tender["tender_id"],
                result.score,
                result.verdict,
                result.reasons,
                result.labels,
            )
            count += 1
        conn.commit()
    finally:
        conn.close()

    logger.info("Tender rescoring completed", extra={"tender_count": count})
    return count
