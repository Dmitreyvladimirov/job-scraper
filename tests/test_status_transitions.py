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


def test_backward_more_than_one_step_rejected():
    assert not is_valid_transition("offer", "found")
    assert not is_valid_transition("interview", "applied")
    assert not is_valid_transition("screen", "found")


def test_backward_one_step_allowed():
    # Drag-and-drop correction: moving a card exactly one step back is a normal
    # kanban operation (e.g. dropped in the wrong column, or a status was premature).
    for i in range(1, len(FUNNEL_ORDER)):
        assert is_valid_transition(FUNNEL_ORDER[i], FUNNEL_ORDER[i - 1])


def test_same_status_rejected():
    assert not is_valid_transition("applied", "applied")


def test_reject_allowed_from_any_stage():
    for status in FUNNEL_ORDER:
        assert is_valid_transition(status, "rejected")


def test_unreject_allowed_to_any_funnel_stage():
    # A card can be rejected from any stage, so there's no single "one step back"
    # target to restore on undo — allow moving out of Rejected into any funnel stage.
    for status in FUNNEL_ORDER:
        assert is_valid_transition("rejected", status)
    assert not is_valid_transition("rejected", "rejected")
    assert not is_valid_transition("rejected", "not_a_real_status")


def test_unknown_status_rejected():
    assert not is_valid_transition("found", "not_a_real_status")
    assert not is_valid_transition("not_a_real_status", "found")


def test_job_never_in_funnel_rejected():
    # current_status=None means outcome != 'qualified' — the job never entered the
    # funnel and must not be reachable through this endpoint, not even to 'rejected'.
    assert not is_valid_transition(None, "rejected")
    assert not is_valid_transition(None, "found")
