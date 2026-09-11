"""Missing history anchors retain a caller-provided, bounded position."""

from cc_remote.tui_state import Block, SessionView


def test_missing_anchor_fallback_is_bounded_and_valid_anchor_wins():
    view = SessionView()
    starts = [
        (0, Block("first", "user", "hello")),
        (20, Block("second", "assistant", "answer")),
    ]
    assert view.resolve(("gone", 0), starts, 50, fallback=30) == 30
    assert view.resolve(("gone", 0), starts, 50, fallback=90) == 50
    assert view.resolve(("gone", 0), starts, 50, fallback=-9) == 0
    assert view.resolve(("gone", 0), [], 0, fallback=90) == 0
    assert view.resolve(("first", 2), starts, 50, fallback=30) == 2
