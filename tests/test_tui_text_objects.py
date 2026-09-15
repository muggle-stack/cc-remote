"""Text objects are local draft operations, never commands or model turns."""

import pytest

from cc_remote.tui_text_objects import text_object


@pytest.mark.parametrize(
    ("text", "cursor", "kind", "around", "selected"),
    [
        ("one two three", 5, "w", False, "two"),
        ("one two three", 5, "w", True, "two "),
        ("one two", 5, "w", True, " two"),
        ("one  two", 3, "w", False, "  "),
        ("one  two", 3, "w", True, "  two"),
        ("foo.bar rest", 2, "W", False, "foo.bar"),
        ("foo.bar rest", 2, "w", False, "foo"),
        ("foo.bar rest", 3, "w", False, "."),
        ("中文 测试", 1, "w", True, "中文 "),
        ("(outer(inner)tail)", 8, "(", False, "inner"),
        ("(outer(inner)tail)", 8, ")", True, "(inner)"),
        ("(outer(inner)tail)", 2, "b", False, "outer(inner)tail"),
        ("(\nhello\n)", 4, "(", False, "\nhello\n"),
        ("[]", 0, "[", False, ""),
        ("[value]", 6, "]", True, "[value]"),
        ("{value}", 3, "B", False, "value"),
        ("<value>", 3, ">", False, "value"),
        ('say "hello" now', 6, '"', False, "hello"),
        ('say "hello" now', 0, '"', True, '"hello"'),
        ("'one' 'two'", 8, "'", False, "two"),
        ('"a\\"b"', 3, '"', False, 'a\\"b'),
        ("`hello`", 2, "`", True, "`hello`"),
    ],
)
def test_text_object_ranges(text, cursor, kind, around, selected):
    span = text_object(text, cursor, kind, around)
    assert span is not None
    assert text[slice(*span)] == selected


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("", "w"),
        ("plain", "("),
        ("(unfinished", "("),
        ('"unfinished', '"'),
        ('"split\nquote"', '"'),
        ("text", "t"),
    ],
)
def test_missing_objects_fail_closed(text, kind):
    assert text_object(text, 0, kind, False) is None
