"""Bound the one-time production migration to the reviewed source and target."""

import pytest

from tests.test_deploy_backend import (
    test_dispatch_migration_is_bounded as check_dispatch,
)
from tests.test_deploy_backend import (
    test_workflow_only_explicit_dispatch_enables_migration as check_event,
)

MERGE = "a85c2f87d019"


@pytest.mark.parametrize(
    "permission,current,head,expected,upgraded",
    [
        ("true", "b6f20a93d812", MERGE, 0, True),
        ("true", MERGE, MERGE, 0, False),
        ("false", "b6f20a93d812", MERGE, 1, False),
        ("true", "e04b75ad6312", MERGE, 1, False),
        ("true", "27f86a5d9c10", MERGE, 1, False),
        ("true", "b6f20a93d812", "27f86a5d9c10", 1, False),
    ],
)
def test_reviewed_merge_is_limited(tmp_path, permission, current, head, expected, upgraded):
    check_dispatch(tmp_path, permission, current, head, expected, upgraded, MERGE)


@pytest.mark.parametrize(
    "event,expected", [("workflow_dispatch", f"true:{MERGE}"), ("push", "false:")]
)
def test_only_explicit_manual_dispatch_can_authorize_merge(event, expected):
    check_event(event, MERGE, expected)


def test_generic_migrate_flag_cannot_authorize_merge(tmp_path):
    check_dispatch(tmp_path, "true", "b6f20a93d812", MERGE, 1, False, "")
