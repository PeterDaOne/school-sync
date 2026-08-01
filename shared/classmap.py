"""
Matching Google Classroom course names to Peter's Notion `For` options.

WHY THIS EXISTS
---------------
Notion's API silently *creates* any select option you hand it that
doesn't already exist. So passing a raw Classroom course name straight
through — "AP Statistics - Period 3" when the Notion option is
"AP Stats" — doesn't fail loudly. It quietly adds a second, nearly
identical option, and from then on Peter's filtered views show half his
Statistics work under one name and half under the other. That failure is
invisible until it has already made a mess.

So: match against the options that already exist, and when nothing
matches confidently, leave `For` empty. An unset field is obvious and
takes two seconds to fix. A near-duplicate option is neither.

`For` USED TO BE `Class` (renamed 2026-07-30)
---------------------------------------------
The property answers "what is this for?", not "which class is this" —
its options are Peter's 8 classes PLUS non-class contexts (School,
Personal, Friends, Work) for the many items that are real commitments
but belong to no course.

That expansion creates a failure mode this module has to defend
against: resolve() fuzzy-matches, so once "Personal" is a valid option,
a Classroom course named "Personal Finance" can match it, and a course
named "Work Experience" can match "Work". A capture sweep would then
file real homework under a life category. NON_CLASS_CATEGORIES is
excluded from matching for exactly that reason — those four are
manual-entry-only, and nothing automated should ever select them.

This module is pure — no network, no Notion — which is why it's testable
and separate from notion_client.
"""

import difflib
import re

from . import config

# Enough similarity to believe two names mean the same class.
MATCH_THRESHOLD = 0.62

# How far ahead the best match must be before we trust it over the
# runner-up. "AP Lang" vs "AP Lit" are close enough that guessing between
# them is worse than leaving the field blank.
AMBIGUITY_MARGIN = 0.08

# Scheduling noise that appears in course names and carries no meaning
# for matching: "AP Statistics - Period 3", "AP Lang (Sec 2) 2026-27".
_NOISE_PATTERNS = [
    r"\bperiod\s*\d+\b",
    r"\bper\s*\d+\b",
    r"\bp\d+\b",
    r"\bsection\s*\d+\b",
    r"\bsec\s*\d+\b",
    r"\bhour\s*\d+\b",
    r"\b\d{4}\s*[-/]\s*\d{2,4}\b",  # 2026-27, 2026/2027
    r"\b(fall|spring|summer|winter)\b",
    r"\b(semester|sem|quarter|qtr|trimester)\s*\d*\b",
    r"\b\d+(st|nd|rd|th)\b",
]


def normalize(name: str) -> str:
    """Lowercase, strip scheduling noise and punctuation, collapse spaces."""
    text = name.lower()
    for pattern in _NOISE_PATTERNS:
        text = re.sub(pattern, " ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _aliases() -> dict[str, str]:
    """
    Explicit overrides for anything the matcher gets wrong, from
    CLASS_ALIASES: "AP Statistics=AP Stats,Honors Lang=AP Lang".

    An escape hatch on purpose — fuzzy matching should handle the normal
    cases, and when it doesn't, Peter needs a way to just say what he
    means without editing code.
    """
    raw = config.optional("CLASS_ALIASES")
    mapping = {}
    for pair in raw.split(","):
        source, sep, target = pair.partition("=")
        if sep and source.strip() and target.strip():
            mapping[normalize(source)] = target.strip()
    return mapping


def resolve(course_name: str | None, options: list[str]) -> str | None:
    """
    Best existing Notion option for a Classroom course name, or None.

    Returning None is a real answer, not a failure — it means "leave the
    For field empty rather than invent an option".

    NON_CLASS_CATEGORIES are filtered out before matching: they are
    Peter's own life categories, not courses, and a fuzzy matcher handed
    "Personal Finance" would happily file it under "Personal". Automated
    capture may only ever choose a class.
    """
    options = [o for o in options if o not in NON_CLASS_CATEGORIES]
    if not course_name or not options:
        return None

    target = normalize(course_name)
    if not target:
        return None

    alias = _aliases().get(target)
    if alias and alias in options:
        return alias

    normalized = {option: normalize(option) for option in options}

    # Exact match after normalization — "AP Stats - Period 3" -> "AP Stats".
    for option, norm in normalized.items():
        if norm and norm == target:
            return option

    # One name containing the other, e.g. "AP US History" inside
    # "AP US History and Government".
    contained = [
        option
        for option, norm in normalized.items()
        if norm and (norm in target or target in norm)
    ]
    if len(contained) == 1:
        return contained[0]

    scored = sorted(
        (
            (difflib.SequenceMatcher(None, target, norm).ratio(), option)
            for option, norm in normalized.items()
            if norm
        ),
        reverse=True,
    )
    if not scored:
        return None

    best_score, best_option = scored[0]
    if best_score < MATCH_THRESHOLD:
        return None
    if len(scored) > 1 and best_score - scored[1][0] < AMBIGUITY_MARGIN:
        return None  # too close to call — better blank than wrong
    return best_option


# The non-course options of `For`. Kept as a frozenset rather than
# inferred (there is no way to tell a class from a life category by
# looking at the string) — if Peter adds another category in the Notion
# UI, it belongs here too, or capture may start matching against it.
NON_CLASS_CATEGORIES = frozenset({"School", "Personal", "Friends", "Work"})


def resolve_category(name: str | None, options: list[str]) -> str | None:
    """
    A non-class category (`School` / `Personal` / `Friends` / `Work`)
    that a CLASSIFIER named outright, or None.

    Deliberately the opposite of resolve(): that one fuzzy-matches course
    names and refuses to return a life category; this one accepts ONLY an
    exact life-category name and never fuzzy-matches anything.

    The split is the whole point (Peter's call, 2026-07-31). The original
    rule — automated capture may never choose a life category — was
    written when the only mechanism was fuzzy-matching a Classroom course
    name, where "Personal Finance" scoring against "Personal" would file
    real homework under a life category, silently and permanently.

    Claude naming a category outright is a different mechanism with a
    different risk. It is not guessing from a course title; it is reading
    an email and saying what the thing is for. The blanket ban cost real
    accuracy — of the five items recovered on 2026-07-31, four (a chore,
    a birthday, a tournament, a porch to sweep) landed with `For` blank
    because there was no way to express "Personal".

    So the hazard is fenced where it actually lives: exact match only,
    only against NON_CLASS_CATEGORIES, and still filtered through the
    live Notion options so a category Peter has since renamed or deleted
    can never be re-created (Notion silently CREATES any select option it
    is handed). A course name cannot reach this function's allow-list,
    and a category cannot reach resolve()'s.
    """
    if not name:
        return None
    candidate = name.strip()
    if candidate not in NON_CLASS_CATEGORIES:
        return None
    return candidate if candidate in options else None

# Keyed by the LIVE Notion option name, not a corrected spelling — Notion
# matches on exact string, so "AP Psycology" (verified via curl 2026-07-30)
# has to be the key until Peter renames the option itself. If he does,
# this dict needs the matching edit or that class silently loses its emoji.
CATEGORY_EMOJI = {
    "AP US History": "🏛️",
    "AP Psycology": "🧠",
    "AP Studio Art": "🎨",
    "AP Pre-Calc": "📐",
    "AP Physics": "⚛️",
    "AP Stats": "📊",
    "AP Lang": "✍️",
    "Leadership": "🧭",
    # Non-class categories (added 2026-07-30 alongside the Class -> For
    # rename). "School" reclaims the 🏫 that used to be prepended to
    # every single notification by the ntfy `school` tag, where it meant
    # nothing; here it distinguishes general school business from a
    # specific course.
    "School": "🏫",
    "Personal": "👤",
    "Friends": "🤝",
    "Work": "💼",
}

# Fallback when an item has no `For` at all, so a categoryless item still
# gets a glyph instead of a bare text title sitting next to its
# neighbours' emoji. Keyed by Notion's `Type`.
TYPE_EMOJI = {
    "Assignments": "📝",
    "Tasks": "☑️",
    "Events": "📅",
}


def category_emoji(name: str | None, type_name: str | None = None) -> str:
    """
    Emoji for a canonical `For` option name, falling back to the item's
    Type, then to "" if neither is known.
    """
    return CATEGORY_EMOJI.get(name or "") or TYPE_EMOJI.get(type_name or "", "")
