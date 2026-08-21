"""Strict structured output schema for tender scoring."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Direction(str, Enum):
    BI_ANALYTICS = "bi_analytics"
    DATA_WAREHOUSES = "data_warehouses"
    BIG_DATA_PLATFORMS = "big_data_platforms"
    MASTER_DATA = "master_data"
    DATA_QUALITY = "data_quality"
    DATABASES = "databases"
    AI_ML = "ai_ml"
    PROCESS_AUTOMATION = "process_automation"
    INFORMATION_SYSTEMS = "information_systems"
    DATA_INTEGRATION = "data_integration"
    NOT_RELEVANT = "not_relevant"
    UNCLEAR = "unclear"


class Uncertainty(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TenderScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=100)
    direction: Direction
    reason: str = Field(min_length=1, max_length=240)
    uncertainty: Uncertainty
