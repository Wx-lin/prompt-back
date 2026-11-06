import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_privilege_defaults_to_no_users():
    assert Settings(_env_file=None).model_dump()["generation_unlimited_user_ids"] == ""


def test_invalid_privilege_configuration_fails_at_startup():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, generation_unlimited_user_ids="invalid")


def test_configured_user_ids_are_normalized(monkeypatch):
    from app.services import generation_privileges
    from app.services.generation_privileges import unlimited_generation_user_ids

    first = uuid.uuid4()
    second = uuid.uuid4()
    monkeypatch.setattr(generation_privileges, "get_settings", lambda: SimpleNamespace(
        generation_unlimited_user_ids=f" {first}, {second}, {first} ",
    ))
    assert unlimited_generation_user_ids() == frozenset({first, second})


def test_default_and_invalid_configuration_fail_closed(monkeypatch):
    from app.services import generation_privileges
    from app.services.generation_privileges import unlimited_generation_user_ids

    monkeypatch.setattr(generation_privileges, "get_settings", lambda: SimpleNamespace(
        generation_unlimited_user_ids="",
    ))
    assert unlimited_generation_user_ids() == frozenset()
    monkeypatch.setattr(generation_privileges, "get_settings", SimpleNamespace)
    assert unlimited_generation_user_ids() == frozenset()
    monkeypatch.setattr(generation_privileges, "get_settings", lambda: SimpleNamespace(
        generation_unlimited_user_ids=f"{uuid.uuid4()},invalid",
    ))
    with pytest.raises(ValueError):
        unlimited_generation_user_ids()
