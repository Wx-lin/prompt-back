"""Bounded search-console snapshot import; positions are source-reported averages."""

from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

Engine = Literal["google", "baidu"]
Dimension = Literal["daily", "query", "page"]


class SeoRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=2048)
    clicks: int = Field(ge=0, le=10**12, strict=True)
    impressions: int = Field(ge=0, le=10**12, strict=True)
    position: float | None = Field(default=None, ge=1, le=1_000_000, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_counts(self):
        if self.clicks > self.impressions:
            raise ValueError("clicks must not exceed impressions")
        return self


class SeoImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: Engine
    dimension: Dimension
    start_date: date
    end_date: date
    rows: list[SeoRow] = Field(max_length=10000)

    @model_validator(mode="after")
    def check_snapshot(self):
        if self.start_date > self.end_date or (self.end_date - self.start_date).days > 365:
            raise ValueError("reporting window must be ordered and at most 366 days")
        if len({row.key for row in self.rows}) != len(self.rows):
            raise ValueError("duplicate keys")
        for row in self.rows:
            if self.dimension == "daily":
                day = date.fromisoformat(row.key)
                if day.isoformat() != row.key or not self.start_date <= day <= self.end_date:
                    raise ValueError("daily key outside reporting window")
            elif self.dimension == "query":
                if len(row.key) > 500 or not row.key.strip() or any(ord(c) < 32 for c in row.key):
                    raise ValueError("invalid query")
            else:
                url = urlsplit(row.key)
                if (
                    url.scheme != "https"
                    or url.netloc != "aigc-prompt.com"
                    or url.fragment
                    or any(c.isspace() for c in row.key)
                ):
                    raise ValueError("page must be a canonical https://aigc-prompt.com URL")
        return self
