"""Readable detail formatting preserves values and terminal safety."""

from cc_remote.tui_details import details
from cc_remote.tui_presentation import SessionPresentation


def test_state_details_are_readable_and_keep_unknown_fields():
    p = SessionPresentation()
    p.context = {"percentage": 43.2055, "tokens": 123456789}
    p.rates = {
        "codex": {
            "secondary": {
                "used_percent": 20.555,
                "window_duration_mins": 10080,
            }
        }
    }
    text = p.panel("Usage / Context")
    assert "Context used: 43%" in text
    assert "Consumed: 21%" in text
    assert "1.23亿 tokens" in text
    assert '"used_percent"' not in text
    assert "New field: No" in details({"new_field": False})
    assert "Zero: 0" in details({"zero": 0})
    assert "\x1b" not in details({"title": "\x1b[31mremote"})
