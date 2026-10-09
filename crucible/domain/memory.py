"""Recall over the shared memory store (hades #208).

Transcripts stay per channel. Decisions and memory are shared by every channel and every
persona. A recall is bounded and newest first: the caller names a subject (free words)
and scope tags, and gets back the current items whose tags overlap the request or whose
text carries one of the subject's words. Nothing superseded or forgotten is recalled.
The SQL repository applies the same rule in the database; this module is the one
definition of it, and the one the tests and the in-memory fakes run."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from crucible.domain.entities import MemoryItem, Role

# Who may promote, supersede, forget, or append a decision: Hades (the orchestrator
# role) or the operator. The admin role is the operator's administrative role and is
# accepted the same way the task note and waiver services accept it. An observer reads.
MEMORY_WRITER_ROLES: frozenset[Role] = frozenset({Role.ORCHESTRATOR, Role.OPERATOR, Role.ADMIN})

RECALL_DEFAULT_LIMIT = 20
RECALL_MAX_LIMIT = 100
MAX_SCOPE_TAGS = 32
MAX_TAG_LENGTH = 64
# A subject word shorter than this says nothing on its own ("a", "to", "of"), and
# neither do these common words, which would otherwise match nearly every text.
MIN_KEYWORD_LENGTH = 3
STOP_WORDS: frozenset[str] = frozenset(
    {
        "about",
        "all",
        "and",
        "any",
        "are",
        "but",
        "can",
        "could",
        "did",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "here",
        "how",
        "into",
        "its",
        "not",
        "our",
        "should",
        "some",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_tag(tag: str) -> str:
    """Tags compare case-insensitively and without surrounding space."""
    return " ".join(tag.strip().lower().split())


def normalize_tags(tags: Iterable[str]) -> list[str]:
    """The distinct, normalized, non-empty tags in their first-seen order."""
    seen: dict[str, None] = {}
    for tag in tags:
        normalized = normalize_tag(tag)
        if normalized:
            seen.setdefault(normalized, None)
    return list(seen)


def subject_keywords(subject: str | None) -> list[str]:
    """The words of a subject a recall matches against the text: lower case, distinct, at
    least MIN_KEYWORD_LENGTH characters long, and not one of the STOP_WORDS."""
    if not subject:
        return []
    seen: dict[str, None] = {}
    for word in _WORDS.findall(subject.lower()):
        if len(word) >= MIN_KEYWORD_LENGTH and word not in STOP_WORDS:
            seen.setdefault(word, None)
    return list(seen)


def matches(item: MemoryItem, *, tags: Sequence[str], keywords: Sequence[str]) -> bool:
    """Whether a current item answers a recall: with neither tags nor keywords, every
    current item does; otherwise one of its tags is asked for, or one of the subject's
    words is in its text."""
    if not item.current:
        return False
    if not tags and not keywords:
        return True
    item_tags = set(normalize_tags(item.scope_tags))
    if item_tags.intersection(normalize_tags(tags)):
        return True
    text = item.text.lower()
    return any(word in text for word in keywords)


def recall(
    items: Iterable[MemoryItem],
    *,
    tags: Sequence[str] = (),
    subject: str | None = None,
    limit: int = RECALL_DEFAULT_LIMIT,
) -> list[MemoryItem]:
    """The matching current items, newest observed first, at most `limit` of them."""
    bounded = max(1, min(int(limit), RECALL_MAX_LIMIT))
    keywords = subject_keywords(subject)
    wanted = [item for item in items if matches(item, tags=tags, keywords=keywords)]
    wanted.sort(key=lambda item: (item.observed_at, item.id), reverse=True)
    return wanted[:bounded]


__all__ = [
    "MAX_SCOPE_TAGS",
    "MAX_TAG_LENGTH",
    "MEMORY_WRITER_ROLES",
    "MIN_KEYWORD_LENGTH",
    "RECALL_DEFAULT_LIMIT",
    "RECALL_MAX_LIMIT",
    "STOP_WORDS",
    "matches",
    "normalize_tag",
    "normalize_tags",
    "recall",
    "subject_keywords",
]
