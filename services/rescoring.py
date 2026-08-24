"""Recalculate tender scores after the company profile changes."""

from __future__ import annotations

import logging
from datetime import datetime

import storage
from icp_config import load_icp
from scoring import _llm_input, score_tender, score_tender_llm

logger = logging.getLogger(__name__)
RESCORE_BATCH_SIZE = 5


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
    scoring_now = datetime.now()
    try:
        rows = storage.query_tenders(conn, limit=None)
        tenders = [dict(row) for row in rows]
        for tender in tenders:
            tender["days_left"] = _days_left(tender.get("deadline"))

        count = 0
        for start in range(0, len(tenders), RESCORE_BATCH_SIZE):
            batch = tenders[start:start + RESCORE_BATCH_SIZE]
            if llm_scorer is not None:
                llm_results = llm_scorer.score_many(_llm_input(tender) for tender in batch)
                if len(llm_results) != len(batch):
                    raise RuntimeError("LLM API вернул неполный пакет оценок.")
                results = [
                    score_tender_llm(
                        tender, profile, llm_result=llm_result, now=scoring_now,
                    )
                    for tender, llm_result in zip(batch, llm_results)
                ]
            else:
                results = [score_tender(tender, profile, now=scoring_now) for tender in batch]

            for tender, result in zip(batch, results):
                storage.update_score(
                    conn,
                    tender["tender_id"],
                    result.score,
                    result.verdict,
                    result.reasons,
                    result.labels,
                )
                count += 1
            # Make progress durable after each group of API calls.
            conn.commit()
    finally:
        conn.close()

    logger.info("Tender rescoring completed", extra={"tender_count": count})
    return count
