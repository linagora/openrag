"""The reasoning level: OpenAI's values, an endpoint's settings, and the level a request is sent with."""

from __future__ import annotations

import pytest
from core.llm.reasoning import REASONING_EFFORTS, resolve_reasoning_effort, validate_reasoning_extra


def test_the_levels_are_openai_s():
    assert REASONING_EFFORTS == ("none", "minimal", "low", "medium", "high", "xhigh")


@pytest.mark.parametrize("effort", [*REASONING_EFFORTS, None])
def test_an_endpoint_may_default_to_any_level_or_none(effort):
    extra = {"reasoning_effort": effort}

    assert validate_reasoning_extra(extra) is extra


@pytest.mark.parametrize("effort", ["max", "High", "", 3, True])
def test_an_endpoint_default_that_is_not_an_openai_level_is_refused(effort):
    with pytest.raises(ValueError, match="extra.reasoning_effort must be one of"):
        validate_reasoning_extra({"reasoning_effort": effort})


def test_a_map_sends_a_level_as_what_the_backend_takes_or_as_none():
    extra = {"reasoning_effort_map": {"low": "high", "xhigh": "max", "none": None}}

    assert validate_reasoning_extra(extra) is extra


@pytest.mark.parametrize(
    ("effort_map", "message"),
    [
        (["low"], "must be an object"),
        ({"max": "high"}, "keys must be among"),
        ({"low": ""}, "must be a non-empty string, or null"),
        ({"low": 1}, "must be a non-empty string, or null"),
    ],
)
def test_a_map_that_no_request_could_use_is_refused(effort_map, message):
    with pytest.raises(ValueError, match=message):
        validate_reasoning_extra({"reasoning_effort_map": effort_map})


def test_an_extra_without_reasoning_settings_is_left_alone():
    assert validate_reasoning_extra(None) is None
    assert validate_reasoning_extra({"temperature": 0.1}) == {"temperature": 0.1}


@pytest.mark.parametrize(
    ("requested", "default", "sent"),
    [
        ("low", "high", "low"),  # the request's wins
        (None, "high", "high"),  # the endpoint's otherwise
        (None, None, None),  # neither: no level, the model's own
    ],
)
def test_the_request_s_level_wins_and_the_endpoint_s_fills_in(requested, default, sent):
    payload = {"temperature": 0.2} if requested is None else {"temperature": 0.2, "reasoning_effort": requested}

    resolve_reasoning_effort(payload, default, None)

    assert payload.get("reasoning_effort") == sent
    assert ("reasoning_effort" in payload) is (sent is not None), "no level is no field, not a null"
    assert payload["temperature"] == 0.2


def test_the_map_rewrites_the_level_whatever_set_it():
    effort_map = {"xhigh": "high", "minimal": "low"}
    requested = {"reasoning_effort": "xhigh"}
    defaulted: dict = {}

    resolve_reasoning_effort(requested, None, effort_map)
    resolve_reasoning_effort(defaulted, "minimal", effort_map)

    assert requested == {"reasoning_effort": "high"}
    assert defaulted == {"reasoning_effort": "low"}


def test_a_level_mapped_to_none_is_not_sent_and_others_go_as_they_are():
    effort_map = {"none": None}
    off = {"reasoning_effort": "none"}
    on = {"reasoning_effort": "medium"}

    resolve_reasoning_effort(off, None, effort_map)
    resolve_reasoning_effort(on, None, effort_map)

    assert off == {}
    assert on == {"reasoning_effort": "medium"}
