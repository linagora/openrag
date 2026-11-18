"""Which messages are small talk, answered without a search."""

import pytest
from core.chat.smalltalk import (
    CasualMessagePolicy,
    acknowledgement_language,
    casual_message_policy,
    classify_latest_message,
    is_casual_reply,
    normalize_casual_message,
)


@pytest.mark.parametrize(
    ("message", "normalized"),
    [
        ("Bonjour ! 👋", "bonjour"),
        ("BONJOUR", "bonjour"),
        ("Hey, how are you?", "hey how are you"),
        ("Merci.", "merci"),
        ("THANK YOU!", "thank you"),
        ("   ?! 👋   ", ""),
        ("Bonjour, qu’est-ce qu’OpenRAG ?", "bonjour qu est ce qu openrag"),
        ("  ÉTÉ\t東京  １２３  ", "été 東京 123"),
        ("👩‍💻 ❤️ 1️⃣", "1"),
    ],
)
def test_normalize_casual_message_preserves_words_while_removing_symbols_and_punctuation(message, normalized):
    assert normalize_casual_message(message) == normalized


@pytest.mark.parametrize(
    ("message", "intent", "language"),
    [
        ("bonjour", "greeting", "fr"),
        ("salut", "greeting", "fr"),
        ("hello", "greeting", "en"),
        ("hey", "greeting", "en"),
        ("comment allez vous", "greeting", "fr"),
        ("how are you", "greeting", "en"),
        ("hey how are you", "greeting", "en"),
        ("comment pouvez-vous m'aider ?", "capability", "fr"),
        ("how can you help me", "capability", "en"),
        ("what can you do", "capability", "en"),
        ("merci", "gratitude", "fr"),
        ("thank you", "gratitude", "en"),
        ("au revoir", "farewell", "fr"),
        ("bye", "farewell", "en"),
    ],
)
def test_casual_message_policy_classifies_every_allowlisted_expression(message, intent, language):
    policy = casual_message_policy(message)

    assert policy is not None
    assert policy.intent == intent
    assert policy.language == language


@pytest.mark.parametrize(
    "message",
    [
        "Bonjour, qu’est-ce qu’OpenRAG ?",
        "Hello, summarize the uploaded document.",
        "Merci, mais que dit le rapport sur ce problème ?",
        "Albendazole is used to treat lymphatic filariasis.",
        "OpenRAG utilise Milvus.",
        "say hello",
        "How can you help me with Product A revenue?",
        "What can you do with the uploaded report?",
    ],
)
def test_casual_message_policy_requires_an_exact_complete_message_match(message):
    assert casual_message_policy(message) is None


# --------------------------------------------------------------------------- #
# classify_latest_message
# --------------------------------------------------------------------------- #


def _user(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def test_a_greeting_is_small_talk():
    assert classify_latest_message(_user("Bonjour !"), "Bonjour !", query_rewrite=True) == (
        CasualMessagePolicy("greeting", "fr"),
        False,
    )


def test_a_question_is_not_small_talk():
    assert classify_latest_message(
        _user("What is the notice period?"), "What is the notice period?", query_rewrite=True
    ) == (
        None,
        False,
    )


@pytest.mark.parametrize("query_rewrite", [True, False])
def test_an_acknowledgement_is_left_to_the_contextualizer(query_rewrite):
    """ "ok" can be thanks or the acceptance of a factual offer: only the history tells."""
    assert classify_latest_message(_user("ok merci"), "ok merci", query_rewrite=query_rewrite) == (None, True)


def test_a_thumbs_up_is_read_in_context_only_when_the_history_is_rewritten():
    assert classify_latest_message(_user("👍🏽"), "👍🏽", query_rewrite=True) == (None, True)
    # Without a rewrite nothing reads the history: the thumbs-up stays small talk.
    assert classify_latest_message(_user("👍🏽"), "👍🏽", query_rewrite=False) == (
        CasualMessagePolicy("empty", "en"),
        True,
    )


def test_a_conversation_ending_with_a_tool_result_is_never_small_talk():
    messages = [
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    assert classify_latest_message(messages, "thanks", query_rewrite=True) == (None, False)


# --------------------------------------------------------------------------- #
# is_casual_reply, acknowledgement_language
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("message", ["merci", "ok", "👍", "  ", "Bye!"])
def test_is_casual_reply_covers_small_talk_and_acknowledgements(message):
    assert is_casual_reply(message)


def test_is_casual_reply_rejects_a_question():
    assert not is_casual_reply("ok, and the notice period?")


def test_acknowledgement_language_prefers_the_last_assistant_turn():
    messages = [
        {"role": "assistant", "content": "Voici le résumé."},
        {"role": "user", "content": "ok"},
    ]
    detected = []

    def detect(text):
        detected.append(text)
        return "fr" if "résumé" in text else None

    assert acknowledgement_language(messages, "ok", detect) == "fr"
    assert detected == ["Voici le résumé."]


def test_acknowledgement_language_falls_back_to_the_message_then_english():
    messages = [{"role": "assistant", "content": ""}, {"role": "user", "content": "ok"}]
    assert acknowledgement_language(messages, "ok", lambda text: "de" if text == "ok" else None) == "de"
    assert acknowledgement_language(messages, "ok", lambda _text: None) == "en"
