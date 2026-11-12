"""Which messages are small talk, answered without a search.

A greeting, thanks, a farewell or a question about the assistant is
recognized only when it is the whole message (``CASUAL_MESSAGE_INTENTS``,
matched after ``normalize_casual_message``). A short acknowledgement ("ok",
"merci", a thumbs-up) is decided by the contextualizer instead, which reads
the history: it can be thanks after a social question, or acceptance of a
factual offer that needs a search.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import NamedTuple


class CasualMessagePolicy(NamedTuple):
    intent: str
    language: str


CASUAL_MESSAGE_INTENTS: dict[str, CasualMessagePolicy] = {
    "bonjour": CasualMessagePolicy("greeting", "fr"),
    "salut": CasualMessagePolicy("greeting", "fr"),
    "hello": CasualMessagePolicy("greeting", "en"),
    "hey": CasualMessagePolicy("greeting", "en"),
    "comment allez vous": CasualMessagePolicy("greeting", "fr"),
    "how are you": CasualMessagePolicy("greeting", "en"),
    "hey how are you": CasualMessagePolicy("greeting", "en"),
    "comment pouvez vous m aider": CasualMessagePolicy("capability", "fr"),
    "how can you help me": CasualMessagePolicy("capability", "en"),
    "what can you do": CasualMessagePolicy("capability", "en"),
    "merci": CasualMessagePolicy("gratitude", "fr"),
    "thank you": CasualMessagePolicy("gratitude", "en"),
    "thanks": CasualMessagePolicy("gratitude", "en"),
    "au revoir": CasualMessagePolicy("farewell", "fr"),
    "bye": CasualMessagePolicy("farewell", "en"),
}
CASUAL_MESSAGES = frozenset(CASUAL_MESSAGE_INTENTS)
_EMPTY_CASUAL_POLICY = CasualMessagePolicy("empty", "en")


def normalize_casual_message(message: str) -> str:
    """Normalize a complete message for strict casual-message matching."""
    normalized = unicodedata.normalize("NFKC", message).casefold()
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        codepoint = ord(character)
        is_variation_selector = 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF
        if category.startswith("S") or category == "Cf" or is_variation_selector or character == "⃣":
            continue
        characters.append(" " if category.startswith("P") else character)
    return " ".join("".join(characters).split())


CASUAL_ACKNOWLEDGEMENTS = frozenset(
    normalize_casual_message(phrase)
    for phrase in (
        "ok",
        "okay",
        "yes",
        "yep",
        "yeah",
        "sure",
        "cool",
        "great",
        "nice",
        "perfect",
        "sounds good",
        "got it",
        "ok thanks",
        "ok thank you",
        "okay thanks",
        "okay thank you",
        "ok merci",
        "okay merci",
        "oui",
        "oui merci",
        "d'accord",
        "d'accord merci",
        "super",
        "super merci",
        "parfait",
        "parfait merci",
    )
)
_THUMB_SKIN_TONES = ("🏻", "🏼", "🏽", "🏾", "🏿")
_THUMBS_UP_REACTIONS = frozenset(
    ("👍", "👍️") + tuple(f"👍{tone}" for tone in _THUMB_SKIN_TONES) + tuple(f"👍️{tone}" for tone in _THUMB_SKIN_TONES)
)


def casual_message_policy(message: str) -> CasualMessagePolicy | None:
    """Return the exact-match casual policy, including empty-input fallback."""
    normalized = normalize_casual_message(message)
    if not normalized:
        return _EMPTY_CASUAL_POLICY
    return CASUAL_MESSAGE_INTENTS.get(normalized)


def is_thumbs_up_reaction(message: str) -> bool:
    """Recognize the common text and skin-tone forms of a thumbs-up reply."""
    return message.strip() in _THUMBS_UP_REACTIONS


def is_casual_reply(message: str) -> bool:
    """True for small talk and for short acknowledgements alike."""
    return (
        casual_message_policy(message) is not None
        or normalize_casual_message(message) in CASUAL_ACKNOWLEDGEMENTS
        or is_thumbs_up_reaction(message)
    )


def acknowledgement_language(messages: list[dict], latest_message: str, detect: Callable[[str], str | None]) -> str:
    """Prefer the last assistant turn's language for very short replies.

    *detect* is the language detector; it returns ``None`` when unsure.
    """
    for message in reversed(messages[:-1]):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            language = detect(content)
            if language:
                return language
    return detect(latest_message) or "en"


class SmallTalk(NamedTuple):
    """How the latest message is classified before the contextualizer runs."""

    #: Answered without a search, unless the request forces one. ``None`` means search.
    policy: CasualMessagePolicy | None
    #: A short acknowledgement: the contextualizer decides, and may return it to small talk.
    acknowledgement_candidate: bool


def classify_latest_message(messages: list[dict], latest_user_message: str, *, query_rewrite: bool) -> SmallTalk:
    """Classify *latest_user_message*, the latest message of the user in *messages*.

    *query_rewrite* is whether the contextualizer rewrites the history: only
    then can a thumbs-up be read in context. A conversation that ends with a
    tool result is never small talk: the model goes on with the result of its
    call, and the request it serves is answered in full, however short it is.
    """
    policy = casual_message_policy(latest_user_message)
    normalized = normalize_casual_message(latest_user_message)
    reaction = is_thumbs_up_reaction(latest_user_message)
    candidate = normalized in CASUAL_ACKNOWLEDGEMENTS or reaction
    if normalized in CASUAL_ACKNOWLEDGEMENTS or (reaction and query_rewrite):
        # Short acknowledgements need the full history: they can be thanks
        # after a social question or acceptance of a factual offer.
        policy = None
    if messages[-1].get("role") == "tool":
        policy = None
        candidate = False
    return SmallTalk(policy, candidate)


__all__ = [
    "CASUAL_ACKNOWLEDGEMENTS",
    "CASUAL_MESSAGES",
    "CASUAL_MESSAGE_INTENTS",
    "CasualMessagePolicy",
    "SmallTalk",
    "acknowledgement_language",
    "casual_message_policy",
    "classify_latest_message",
    "is_casual_reply",
    "is_thumbs_up_reaction",
    "normalize_casual_message",
]
