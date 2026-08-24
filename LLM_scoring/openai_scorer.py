"""Reusable LLM service for tender relevance scoring."""

from __future__ import annotations

import os
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from dotenv import load_dotenv
from openai import OpenAI

from .prompts import build_system_prompt
from .schemas import TenderScore


DEFAULT_CONCURRENCY = 5
MAX_TITLE_LENGTH = 2_000

load_dotenv()
logger = logging.getLogger(__name__)


class ResponsesAPI(Protocol):
    def parse(self, **kwargs: Any) -> Any: ...


class ResponsesClient(Protocol):
    """Minimal OpenAI-compatible client contract used by the scorer."""
    responses: ResponsesAPI


@dataclass(frozen=True, slots=True)
class ScoringResult:
    title: str
    evaluation: TenderScore
    model: str
    input_tokens: int | None
    output_tokens: int | None

    def to_dict(self) -> dict[str, object]:
        evaluation = self.evaluation.model_dump(mode="json")
        return {
            "title": self.title,
            "evaluation": evaluation,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class OpenAITenderScorer:
    def __init__(
        self,
        *,
        client: ResponsesClient | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency должен быть не меньше 1.")

        if client is None:
            resolved_api_key = api_key or os.getenv("OPENAI_API_KEY")
            if not resolved_api_key:
                raise RuntimeError("Не задана переменная окружения OPENAI_API_KEY.")
            client = OpenAI(
                api_key=resolved_api_key,
                base_url=base_url or os.getenv("OPENAI_BASE_URL"),
                timeout=30.0,
                max_retries=2,
            )

        self.client = client
        self.model = model or os.getenv("OPENAI_TENDER_MODEL")
        if not self.model:
            raise RuntimeError("Не задана переменная окружения OPENAI_TENDER_MODEL.")
        self.max_concurrency = max_concurrency
        self.system_prompt = build_system_prompt()

    def score(self, title: str) -> ScoringResult:
        """Вернуть оценку одного тендера"""
        clean_title = _validate_title(title)
        return self._score_clean_title(clean_title)

    def _score_clean_title(self, clean_title: str) -> ScoringResult:
        response = self.client.responses.parse(
            model=self.model,
            reasoning={"effort": "none"},
            store=False,
            input=[
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": f"Данные тендера:\n{clean_title}",
                },
            ],
            text_format=TenderScore,
        )

        evaluation = response.output_parsed
        if evaluation is None:
            raise RuntimeError("LLM API не вернул структурированную оценку.")

        usage = getattr(response, "usage", None)
        result = ScoringResult(
            title=clean_title,
            evaluation=evaluation,
            model=self.model,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        logger.info(
            "llm_tender_scored model=%s input_tokens=%s output_tokens=%s",
            result.model, result.input_tokens, result.output_tokens,
        )
        return result

    def score_many(self, titles: Iterable[str]) -> list[ScoringResult]:
        """Параллельная оценка множества тендеров"""
        clean_titles = [_validate_title(title) for title in titles]
        if not clean_titles:
            return []

        workers = min(self.max_concurrency, len(clean_titles))
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="tender-scoring",
        ) as executor:
            return list(executor.map(self._score_clean_title, clean_titles))


def _validate_title(title: str) -> str:
    clean_title = str(title).strip()
    if not clean_title:
        raise ValueError("Название тендера не может быть пустым.")
    if len(clean_title) > MAX_TITLE_LENGTH:
        raise ValueError(
            f"Название тендера длиннее допустимых {MAX_TITLE_LENGTH} символов."
        )
    return clean_title
