import pytest
from pydantic import ValidationError

from app.config import Settings


def test_analysis_feature_is_disabled_by_default():
    settings = Settings(_env_file=None)
    assert settings.model_dump()["prompt_analysis_enabled"] is False
    assert settings.model_dump()["prompt_analysis_worker_enabled"] is False


def test_analysis_limits_are_validated():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, prompt_analysis_daily_limit=0)
