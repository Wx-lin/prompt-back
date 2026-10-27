"""Google's explicit zero-exposure rows have no measurable search position."""

from pydantic import model_validator

from app.schemas.seo import SeoRow


class GoogleSourceRow(SeoRow):
    @model_validator(mode="before")
    @classmethod
    def normalize_unmeasured_position(cls, value):
        if (
            isinstance(value, dict)
            and type(value.get("impressions")) in (int, float)
            and value["impressions"] == 0
            and type(value.get("position")) in (int, float)
            and value["position"] == 0
        ):
            return {**value, "position": None}
        return value
