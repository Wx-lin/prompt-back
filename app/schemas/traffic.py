from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VisitInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    visitor_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$", min_length=64, max_length=64)

    @field_validator("visitor_hash")
    @classmethod
    def normalize_hash(cls, value: str) -> str:
        return value.lower()


class DailyTraffic(BaseModel):
    date: str
    uv: int


class TrafficSummary(BaseModel):
    generated_at: datetime
    tracking_since: datetime | None
    timezone: Literal["Asia/Shanghai"]
    today_uv: int
    seven_day_uv: int
    thirty_day_uv: int
    daily: list[DailyTraffic]
