import pytest
from core.utils.string_utils import str_to_bool


def test_str_to_bool_true_values():
    """Test that valid 'true' strings are converted to True."""
    assert str_to_bool("true") is True
    assert str_to_bool("True") is True
    assert str_to_bool("TRUE") is True
    assert str_to_bool("1") is True
    assert str_to_bool("t") is True
    assert str_to_bool("T") is True
    assert str_to_bool("y") is True
    assert str_to_bool("yes") is True
    assert str_to_bool("YES") is True
    assert str_to_bool("on") is True
    assert str_to_bool("ON") is True


def test_str_to_bool_false_values():
    """Test that valid 'false' strings are converted to False."""
    assert str_to_bool("false") is False
    assert str_to_bool("False") is False
    assert str_to_bool("FALSE") is False
    assert str_to_bool("0") is False
    assert str_to_bool("f") is False
    assert str_to_bool("F") is False
    assert str_to_bool("n") is False
    assert str_to_bool("no") is False
    assert str_to_bool("NO") is False
    assert str_to_bool("off") is False
    assert str_to_bool("OFF") is False


def test_str_to_bool_strips_whitespace():
    """Test that leading/trailing whitespace is ignored."""
    assert str_to_bool("  true  ") is True
    assert str_to_bool("\tfalse\n") is False
    assert str_to_bool(" yes ") is True
    assert str_to_bool(" 0 ") is False


def test_str_to_bool_mixed_case_and_whitespace():
    """Test combination of mixed case and surrounding whitespace."""
    assert str_to_bool("  YeS  ") is True
    assert str_to_bool("  No  ") is False


def test_str_to_bool_invalid_string_raises():
    """Test that an unrecognized string raises ValueError."""
    with pytest.raises(ValueError):
        str_to_bool("maybe")


def test_str_to_bool_empty_string_raises():
    """Test that an empty string raises ValueError."""
    with pytest.raises(ValueError):
        str_to_bool("")


def test_str_to_bool_whitespace_only_raises():
    """Test that a whitespace-only string raises ValueError."""
    with pytest.raises(ValueError):
        str_to_bool("   ")


def test_str_to_bool_numeric_like_but_invalid_raises():
    """Test that numeric-looking but unsupported values raise ValueError."""
    with pytest.raises(ValueError):
        str_to_bool("2")
    with pytest.raises(ValueError):
        str_to_bool("-1")
    with pytest.raises(ValueError):
        str_to_bool("1.0")


def test_str_to_bool_none_raises_typeerror():
    """Test that passing None raises an error (no .strip() on None)."""
    with pytest.raises(AttributeError):
        str_to_bool(None)


def test_str_to_bool_non_string_type_raises():
    """Test that a non-string type without .strip()/.lower() raises an error."""
    with pytest.raises(AttributeError):
        str_to_bool(123)


def test_str_to_bool_similar_but_wrong_word_raises():
    """Test words that resemble valid values but aren't exact matches."""
    with pytest.raises(ValueError):
        str_to_bool("yeah")
    with pytest.raises(ValueError):
        str_to_bool("nah")
    with pytest.raises(ValueError):
        str_to_bool("truee")
