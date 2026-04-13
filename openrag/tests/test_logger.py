from utils.logger import escape_markup


def test_escape_markup_escapes_angle_brackets():
    s = "From <notifications@github.com>"
    out = escape_markup(s)
    assert out == r"From \<notifications@github.com\>"


def test_escape_markup_escapes_backslashes_first():
    s = r"path\to\file <tag>"
    out = escape_markup(s)
    # backslashes doubled + angle brackets escaped
    assert out == r"path\\to\\file \<tag\>"
