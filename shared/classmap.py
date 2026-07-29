"""
Matching Google Classroom course names to Peter's Notion `Class` options.

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
matches confidently, leave `Class` empty. An unset field is obvious and
takes two seconds to fix. A near-duplicate option is neither.

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
    Class field empty rather than invent an option".
    """
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


# Keyed by the LIVE Notion option name, not a corrected spelling — Notion
# matches on exact string, so "AP Psycology" (verified via curl 2026-07-28)
# has to be the key until Peter renames the option itself. If he does,
# this dict needs the matching edit or that class silently loses its emoji.
CLASS_EMOJI = {
    "AP US History": "🏛️",
    "AP Psycology": "🧠",
    "AP Studio Art": "🎨",
    "AP Pre-Calc": "📐",
    "AP Physics": "⚛️",
    "AP Stats": "📊",
    "AP Lang": "✍️",
    "Leadership": "🧭",
}


def class_emoji(name: str | None) -> str:
    """Emoji for a canonical Class option name, or "" if unset/unknown."""
    return CLASS_EMOJI.get(name or "", "")
