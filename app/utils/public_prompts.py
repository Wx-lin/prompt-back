"""Shared publication boundary for every public prompt read."""

from sqlalchemy import and_, or_

from app.models.prompt import Prompt


def public_prompt_condition():
    return and_(
        Prompt.is_published.is_(True), or_(Prompt.review_status.is_(None), Prompt.review_status == "approved")
    )
