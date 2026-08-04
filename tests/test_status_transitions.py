import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

from config import FUNNEL_ORDER, is_valid_transition  # noqa: E402


def test_forward_progression_allowed():
    for i in range(len(FUNNEL_ORDER) - 1):
        assert is_valid_transition(FUNNEL_ORDER[i], FUNNEL_ORDER[i + 1])


def test_skipping_stages_forward_allowed():
    assert is_valid_transition("found", "interview")
    assert is_valid_transition("applied", "offer")


def test_backward_transition_rejected():
    assert not is_valid_transition("offer", "found")
    assert not is_valid_transition("interview", "applied")
    assert not is_valid_transition("screen", "found")


def test_same_status_rejected():
    assert not is_valid_transition("applied", "applied")


def test_reject_allowed_from_any_stage():
    for status in FUNNEL_ORDER:
        assert is_valid_transition(status, "rejected")


def test_cannot_move_away_from_rejected():
    for status in FUNNEL_ORDER:
        assert not is_valid_transition("rejected", status)
    assert not is_valid_transition("rejected", "rejected")


def test_unknown_status_rejected():
    assert not is_valid_transition("found", "not_a_real_status")
    assert not is_valid_transition("not_a_real_status", "found")


def test_job_never_in_funnel_rejected():
    # current_status=None means outcome != 'qualified' — the job never entered the
    # funnel and must not be reachable through this endpoint, not even to 'rejected'.
    assert not is_valid_transition(None, "rejected")
    assert not is_valid_transition(None, "found")
