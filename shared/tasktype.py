"""
Inferring `Task Type` and `Priority` for items the capture sweeps create.

WHY THIS EXISTS
---------------
Every item Peter types by hand gets a Task Type and a Priority. Items the
capture sweeps create had neither, so a captured assignment showed up
visibly less complete than a typed one -- and `Task Type` is what his
filtered views key on.

PETER'S CONVENTION, READ OFF HIS REAL DATA (2026-07-30)
-------------------------------------------------------
One VERB plus one NOUN:

    Buy book        -> Execute + Action
    Eesay           -> Execute + Essay/Writing
    Algebra Work    -> Execute + Problem Set
    Pysics Lab      -> Execute + Lab
    Reading book    -> Execute + Reading
    Prom            -> Attend
    Meeting         -> Attend

Verbs are Execute / Attend / Remember; everything else in the property is
a noun. `Action` is the generic noun, not a verb -- "Buy book" is
Execute + Action.

THE SAME NOTION HAZARD AS classmap
-----------------------------------
`Task Type` is a multi-select and `Priority` a select, and Notion
SILENTLY CREATES any option you hand it that doesn't already exist -- so
a typo here would quietly add a fourteenth Task Type option next to the
thirteen real ones, and split his views. Everything returned from this
module is therefore filtered against the options that actually exist
(notion_client.select_option_names), exactly like classmap.resolve. An
unmatched guess is dropped, never invented.

PRIORITY IS DELIBERATELY HIGH-OR-MEDIUM, NEVER LOW
---------------------------------------------------
Peter's call, 2026-07-30. Two things make this the conservative choice:

  - Priority MULTIPLIES the reminder interval (High 0.5x, Medium 1.0x,
    Low 2.0x), so a wrong High doubles that item's push rate. The
    keyword list for High is therefore short and specific -- real
    assessments and major deliverables only. Everything else is Medium.
  - Low is excluded on purpose: it doubles the interval, and an
    automated guess should not be able to make an item nag LESS than
    the default. Peter can still set Low by hand.

The due-date urgency is already handled by the cadence formula
(interval = alpha * days_until), so nothing here looks at the due date --
that would double-count the one signal the engine already reads.
"""

import re

# The three verbs. Everything else in the property is a noun.
VERBS = ("Execute", "Attend", "Remember")

# Generic noun, used when no specific one matches. NOT a verb -- see the
# module docstring; "Buy book" is Execute + Action in Peter's own data.
FALLBACK_NOUN = "Action"

# Noun tags, most specific first. Order matters: "lab report" should be a
# Lab, not an Essay/Writing, and "research paper" should be Research
# rather than matching "paper" as Essay/Writing.
_NOUN_PATTERNS: list[tuple[str, str]] = [
    ("Lab", r"\blabs?\b|\blab\s*report\b|\bexperiment\b|\bdissection\b"),
    ("Research", r"\bresearch\b|\bbibliograph|\bsources?\b|\bcitations?\b|\bannotated\b"),
    ("Test/Quiz Prep", r"\btests?\b|\bquiz(?:zes)?\b|\bexams?\b|\bmidterms?\b|\bfinals?\b"
                       r"|\bassessment\b|\bstudy\s*guide\b|\breview\s*(?:sheet|packet)\b"),
    ("Essay/Writing", r"\bessays?\b|\bpapers?\b|\bwrit(?:e|ing|ten)\b|\bcompositions?\b"
                      r"|\bnarrative\b|\brhetorical\b|\banalysis\b|\bjournal\b|\breflection\b"
                      r"|\bdrafts?\b|\bthesis\b"),
    ("Presentation", r"\bpresentations?\b|\bpresent\b|\bslides?\b|\bdecks?\b|\bpowerpoint\b"
                     r"|\bspeech\b|\bposter\b"),
    # Math subject names are here rather than in a generic "homework"
    # pattern: "Algebra Work" is a problem set, but a bare \bhomework\b
    # would swallow "Reading homework" too, since Problem Set is matched
    # before Reading.
    ("Problem Set", r"\bproblem\s*sets?\b|\bp-?set\b|\bworksheets?\b|\bproblems?\b"
                    r"|\bexercises?\b|\bequations?\b|\bpractice\b|\bdrills?\b"
                    r"|\balgebra\b|\bcalculus\b|\bgeometr(?:y|ic)\b|\btrig(?:onometry)?\b"
                    r"|\bderivatives?\b|\bintegrals?\b|\bpolynomials?\b"),
    # NOT a bare \bbook\b: "Buy book" is Execute + Action in Peter's own
    # data, and matching it as Reading was wrong. Reading needs an actual
    # reading signal -- the verb, a chapter, or a thing you read.
    ("Reading", r"\bread(?:ing|ings)?\b|\bchapters?\b|\bch\.\s*\d|\bpages?\b|\bpp?\.\s*\d"
                r"|\bnovel\b|\barticle\b|\btextbook\b|\bannotate\b"),
    ("Project", r"\bprojects?\b|\bbuild\b|\bdesign\b|\bportfolio\b|\bmodel\b"),
    ("Meeting", r"\bmeetings?\b|\bconferences?\b|\boffice\s*hours\b|\bcheck-?in\b"),
]

# Verb overrides. Execute is the default for anything the capture sweeps
# create (it's work he has to do); these are the cases where it isn't.
#
# Attend: he goes somewhere at a time, rather than producing something.
# Remember: nothing to produce and nowhere to be -- just don't forget.
_ATTEND = (
    r"\bmeetings?\b|\bconferences?\b|\bfield\s*trip\b|\bassembl(?:y|ies)\b"
    r"|\brehearsals?\b|\bconcerts?\b|\bperformances?\b|\bgames?\b|\bmatch\b"
    r"|\bceremony\b|\bbanquet\b|\bseminars?\b|\bworkshops?\b|\blectures?\b"
    r"|\boffice\s*hours\b|\breview\s*session\b|\bstudy\s*session\b|\btutoring\b"
    r"|\battend\b|\bshow\s*up\b"
)
_REMEMBER = (
    r"\bbring\b|\bpermission\s*slip\b|\bsign(?:ed)?\s*(?:form|slip|sheet)\b"
    r"|\bremember\b|\bdon'?t\s*forget\b|\breminder\b|\bnote\s*that\b"
    r"|\bwear\b|\bpack\b|\bdeadline\s*only\b|\bregister\b|\brsvp\b|\bpay\b|\bfee\b"
)

# HIGH-priority signals, deliberately short. Each one has to be worth
# doubling the item's notification rate -- see the module docstring.
# Matches Peter's own tagging: essays, labs and Algebra Work are High;
# plain reading is not.
_HIGH = (
    r"\btests?\b|\bquiz(?:zes)?\b|\bexams?\b|\bmidterms?\b|\bfinals?\b"
    r"|\bassessment\b|\bessays?\b|\bpapers?\b|\bprojects?\b|\bpresentations?\b"
    r"|\bthesis\b|\blab\s*report\b|\bsummative\b|\bgraded\b"
)

# Type -> default verb, before any keyword override. Events are somewhere
# he goes; Assignments and Tasks are work he does.
_TYPE_VERB = {"Events": "Attend", "Assignments": "Execute", "Tasks": "Execute"}


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def verb(title: str | None, type_name: str | None = None) -> str:
    """
    The verb tag: what Peter has to DO with this item.

    Keyword matches win over the type default, so a Classroom item called
    "Field Trip Permission Slip" is Remember rather than Execute even
    though everything Classroom sends is typed Assignments.
    """
    text = _norm(title)
    if text:
        # Remember first: "bring your permission slip to the assembly"
        # is something to not forget, not something to attend.
        if re.search(_REMEMBER, text):
            return "Remember"
        if re.search(_ATTEND, text):
            return "Attend"
    return _TYPE_VERB.get(type_name or "", "Execute")


def noun(title: str | None) -> str:
    """The noun tag: what KIND of thing it is. Falls back to Action."""
    text = _norm(title)
    for name, pattern in _NOUN_PATTERNS:
        if text and re.search(pattern, text):
            return name
    return FALLBACK_NOUN


def resolve(
    title: str | None, type_name: str | None, options: list[str]
) -> list[str]:
    """
    The Task Type tags for a captured item: one verb plus one noun,
    filtered to options that already exist in Notion.

    Filtering is the whole point -- Notion creates any multi-select
    option it is handed, so a tag that isn't already defined is dropped
    rather than sent. Returns [] when nothing matches, which leaves the
    property empty (obvious, and two seconds to fix) instead of polluted.
    """
    available = set(options)
    tags = [t for t in (verb(title, type_name), noun(title)) if t in available]
    # dict.fromkeys rather than set(): order is verb-then-noun, and a
    # title that somehow produced the same tag twice shouldn't duplicate.
    return list(dict.fromkeys(tags))


def priority(title: str | None, options: list[str]) -> str | None:
    """
    "High" for real assessments and major deliverables, "Medium"
    otherwise. Never "Low" -- see the module docstring.

    Returns None if neither option exists in Notion, so the caller leaves
    the field unset rather than inventing one. Unset already behaves as
    Medium in the reminder engine, so that degrades cleanly.
    """
    text = _norm(title)
    wanted = "High" if text and re.search(_HIGH, text) else "Medium"
    if wanted in options:
        return wanted
    return "Medium" if "Medium" in options else None
