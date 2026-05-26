from utils.logger import escape_markup, mask_email


def test_mask_email_keeps_first_char_and_domain():
    assert mask_email("alice@example.com") == "a***@example.com"


def test_mask_email_redacts_short_and_uppercase_local_parts():
    assert mask_email("a@example.com") == "a***@example.com"
    assert mask_email("Bob.Smith@corp.io") == "B***@corp.io"


def test_mask_email_handles_missing_or_malformed_values():
    assert mask_email(None) == "***"
    assert mask_email("") == "***"
    assert mask_email("not-an-email") == "***"
    assert mask_email(123) == "***"


def test_escape_markup_escapes_angle_brackets():
    s = "From <notifications@github.com>"
    out = escape_markup(s)
    assert out == r"From \<notifications@github.com\>"


def test_escape_markup_escapes_backslashes_first():
    s = r"path\to\file <tag>"
    out = escape_markup(s)
    # backslashes doubled + angle brackets escaped
    assert out == r"path\\to\\file \<tag\>"
