import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

from config import USER_REJECTION_REASONS, validate_status_change  # noqa: E402


def test_reject_without_reason_is_invalid():
    assert validate_status_change("found", "rejected", None) is not None
    assert validate_status_change("found", "rejected", "") is not None


def test_all_user_facing_reasons_are_valid():
    for reason in USER_REJECTION_REASONS:
        assert validate_status_change("applied", "rejected", reason) is None


def test_geo_restricted_auto_not_settable_via_user_form():
    assert validate_status_change("found", "rejected", "geo_restricted_auto") is not None


def test_old_not_settable_via_user_form():
    # "old" is set by bulk maintenance scripts (Notion sync, stale-backlog cleanup)
    # reconciling cards that were never individually reviewed — not a reason a human
    # picks in the reject form.
    assert validate_status_change("found", "rejected", "old") is not None


def test_unknown_reason_is_invalid():
    assert validate_status_change("found", "rejected", "made_up_reason") is not None


def test_reason_ignored_for_non_reject_transitions():
    # A stray rejection_reason shouldn't block a normal forward move.
    assert validate_status_change("found", "applied", "low_score_after_review") is None
