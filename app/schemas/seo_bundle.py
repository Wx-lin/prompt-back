"""Strict, versioned interchange for the fixed read-only Google property."""

from datetime import date, timedelta
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.google_search_console import GoogleSourceRow
from app.schemas.seo import Dimension


class SeoPartition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: date
    dimension: Dimension
    rows: list[GoogleSourceRow] = Field(max_length=50000)
    truncated: bool = Field(strict=True)

    @model_validator(mode="after")
    def validate_rows(self):
        if len({row.key for row in self.rows}) != len(self.rows):
            raise ValueError("duplicate source keys")
        if self.dimension == "daily" and (
            len(self.rows) > 1 or any(row.key != self.day.isoformat() for row in self.rows)
        ):
            raise ValueError("daily key mismatch")
        for row in self.rows:
            if self.dimension == "page":
                parsed = urlsplit(row.key)
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != "aigc-prompt.com"
                    or parsed.fragment
                    or any(c.isspace() for c in row.key)
                ):
                    raise ValueError("invalid source page")
            if self.dimension == "query" and (
                len(row.key) > 500 or not row.key.strip() or any(ord(c) < 32 for c in row.key)
            ):
                raise ValueError("invalid source query")
        return self


class SeoBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    site_url: Literal["https://aigc-prompt.com/"]
    search_type: Literal["web"]
    data_state: Literal["final"]
    previous_data_through: date | None
    target_end: date
    start_date: date
    end_date: date
    partitions: list[SeoPartition] = Field(min_length=3, max_length=84)

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("version must be integer")
        return value

    @model_validator(mode="after")
    def check_window(self):
        from app.services.seo_sync_service import sync_window

        end, days = sync_window(self.previous_data_through, self.target_end)
        if self.previous_data_through and self.previous_data_through > self.target_end:
            raise ValueError("future frontier")
        if self.end_date != end or self.start_date != end - timedelta(days=days - 1):
            raise ValueError("invalid reporting window")
        expected = {
            (self.start_date + timedelta(days=i), dimension)
            for i in range(days)
            for dimension in ("daily", "query", "page")
        }
        actual = {(part.day, part.dimension) for part in self.partitions}
        if actual != expected or len(actual) != len(self.partitions):
            raise ValueError("incomplete or duplicate partitions")
        return self
