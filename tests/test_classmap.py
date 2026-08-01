"""
Tests for Classroom course name -> Notion Class option matching.

The failure this guards against is silent: Notion auto-creates any
select option it's handed, so a bad match doesn't error, it just
quietly splits one class across two options. These use Peter's real
Notion options so the cases are the ones he'll actually hit.
"""

import os
import unittest

import tests.context  # noqa: F401

from shared import classmap

# The live options as of 2026-07-28, verified by raw curl.
OPTIONS = [
    "AP Stats",
    "Leadership",
    "AP Studio Art",
    "AP Lang",
    "AP US History",
    "AP Psycology",  # Peter's spelling — matching must not depend on it
    "AP Pre-Calc",
    "AP Physics",
]


class Normalize(unittest.TestCase):
    def test_strips_period_suffixes(self):
        self.assertEqual(classmap.normalize("AP Stats - Period 3"), "ap stats")
        self.assertEqual(classmap.normalize("AP Lang (P2)"), "ap lang")
        self.assertEqual(classmap.normalize("AP Physics Sec 4"), "ap physics")

    def test_strips_school_year_and_term(self):
        self.assertEqual(classmap.normalize("AP Stats 2026-27"), "ap stats")
        self.assertEqual(classmap.normalize("Leadership Fall Semester"), "leadership")

    def test_collapses_punctuation_and_spacing(self):
        self.assertEqual(classmap.normalize("AP  US   History!!"), "ap us history")


class Resolve(unittest.TestCase):
    def r(self, name):
        return classmap.resolve(name, OPTIONS)

    def test_exact_option_passes_through(self):
        self.assertEqual(self.r("AP Stats"), "AP Stats")

    def test_period_suffix_is_ignored(self):
        self.assertEqual(self.r("AP Stats - Period 3"), "AP Stats")

    def test_case_and_punctuation_insensitive(self):
        self.assertEqual(self.r("ap  physics"), "AP Physics")

    def test_expanded_course_name_matches_abbreviation(self):
        # The common real case: Classroom says the full name, Notion has
        # Peter's shorthand.
        self.assertEqual(self.r("AP Statistics"), "AP Stats")

    def test_matches_despite_peters_typo_in_the_notion_option(self):
        self.assertEqual(self.r("AP Psychology"), "AP Psycology")

    def test_containment_match(self):
        self.assertEqual(self.r("AP US History and Government"), "AP US History")

    def test_unknown_class_returns_none_rather_than_inventing(self):
        """The whole point: never hand Notion an option it doesn't have."""
        self.assertIsNone(self.r("Marching Band"))

    def test_ambiguous_match_refuses_to_guess(self):
        # Two options equally plausible (identical scores) — blank beats
        # a coin flip, because a wrong Class is invisible once written.
        self.assertIsNone(
            classmap.resolve("AP Physic", ["AP Physics 1", "AP Physics 2"])
        )

    def test_two_containing_options_also_refuse_to_guess(self):
        self.assertIsNone(classmap.resolve("Art", ["AP Studio Art", "Art History"]))

    def test_unique_containment_still_wins(self):
        # Only one option contains it, so this is not ambiguous.
        self.assertEqual(classmap.resolve("AP Lan", ["AP Lang", "AP Lit"]), "AP Lang")

    def test_empty_inputs(self):
        self.assertIsNone(classmap.resolve(None, OPTIONS))
        self.assertIsNone(classmap.resolve("", OPTIONS))
        self.assertIsNone(classmap.resolve("AP Stats", []))

    def test_name_that_normalizes_to_nothing(self):
        self.assertIsNone(self.r("Period 3"))


class Aliases(unittest.TestCase):
    def setUp(self):
        self._original = os.environ.get("CLASS_ALIASES")

    def tearDown(self):
        if self._original is None:
            os.environ.pop("CLASS_ALIASES", None)
        else:
            os.environ["CLASS_ALIASES"] = self._original

    def test_alias_overrides_matching(self):
        os.environ["CLASS_ALIASES"] = "Quantitative Reasoning=AP Stats"
        self.assertEqual(classmap.resolve("Quantitative Reasoning", OPTIONS), "AP Stats")

    def test_alias_ignored_when_target_is_not_a_real_option(self):
        os.environ["CLASS_ALIASES"] = "Marching Band=Band"
        self.assertIsNone(classmap.resolve("Marching Band", OPTIONS))

    def test_alias_tolerates_spacing_and_period_suffix(self):
        os.environ["CLASS_ALIASES"] = " Quantitative Reasoning = AP Stats "
        self.assertEqual(
            classmap.resolve("Quantitative Reasoning - Period 5", OPTIONS), "AP Stats"
        )

    def test_malformed_alias_string_is_ignored(self):
        os.environ["CLASS_ALIASES"] = "garbage,,=,x="
        self.assertEqual(classmap.resolve("AP Stats", OPTIONS), "AP Stats")


class CategoryEmoji(unittest.TestCase):
    def test_every_live_option_has_an_emoji(self):
        """
        Every canonical name Resolve can return should have an emoji, or
        a notification silently loses its visual identifier for that
        class with no error anywhere.
        """
        for option in OPTIONS:
            self.assertNotEqual(classmap.category_emoji(option), "", option)

    def test_unknown_class_returns_empty_not_a_placeholder(self):
        self.assertEqual(classmap.category_emoji("Marching Band"), "")

    def test_none_returns_empty(self):
        self.assertEqual(classmap.category_emoji(None), "")

    def test_misspelled_live_option_still_resolves(self):
        # The dict is keyed by the LIVE (misspelled) Notion option name
        # on purpose -- resolve() would hand this exact string back.
        self.assertEqual(classmap.category_emoji("AP Psycology"), "🧠")

    def test_non_class_categories_have_emoji(self):
        for name in classmap.NON_CLASS_CATEGORIES:
            self.assertNotEqual(classmap.category_emoji(name), "", name)

    def test_falls_back_to_type_when_category_unset(self):
        """A categoryless item still gets a glyph, so it doesn't sit in the
        notification list as the one bare text title among emoji."""
        self.assertEqual(classmap.category_emoji(None, "Tasks"), "☑️")
        self.assertEqual(classmap.category_emoji("", "Events"), "📅")

    def test_category_wins_over_type_fallback(self):
        self.assertEqual(classmap.category_emoji("AP Lang", "Assignments"), "✍️")

    def test_unknown_category_and_unknown_type_is_empty(self):
        self.assertEqual(classmap.category_emoji("Marching Band", "Nonsense"), "")


# The `For` property holds Peter's classes AND his life categories
# (School / Personal / Friends / Work). Automated capture must never
# select one of the latter: a fuzzy matcher handed a real course name
# will otherwise file homework under a life category, silently and
# permanently. These are the cases that motivated the exclusion.
CATEGORY_OPTIONS = OPTIONS + ["School", "Personal", "Friends", "Work"]


class NonClassCategoriesAreNeverAutoSelected(unittest.TestCase):
    def r(self, name):
        return classmap.resolve(name, CATEGORY_OPTIONS)

    def test_personal_finance_course_does_not_match_personal(self):
        self.assertNotEqual(self.r("Personal Finance"), "Personal")

    def test_work_experience_course_does_not_match_work(self):
        self.assertNotEqual(self.r("Work Experience"), "Work")

    def test_exact_category_name_is_still_refused(self):
        """Even a course literally named "School" must not select it —
        the exclusion is on the option, not on match confidence."""
        self.assertIsNone(self.r("School"))
        self.assertIsNone(self.r("Personal"))

    def test_no_course_name_ever_resolves_to_a_non_class_category(self):
        for name in ("Personal Finance", "Work Experience", "School of Rock",
                     "Friends Seminar", "Personal Fitness", "Work Study"):
            self.assertNotIn(self.r(name), classmap.NON_CLASS_CATEGORIES, name)

    def test_real_classes_still_resolve_alongside_the_categories(self):
        """The exclusion must not damage the matching it sits next to."""
        self.assertEqual(self.r("AP Statistics - Period 3"), "AP Stats")
        self.assertEqual(self.r("AP Lang"), "AP Lang")

    def test_only_categories_available_means_no_match(self):
        self.assertIsNone(classmap.resolve("AP Stats", ["School", "Personal"]))


class ExplicitCategoryFromTheClassifier(unittest.TestCase):
    """
    resolve_category is the OTHER half of the 2026-07-31 split: a
    classifier may name a life category outright, because that is not the
    mechanism the blanket ban was defending against.

    The property that must survive is the one the ban existed for: a
    COURSE NAME still cannot reach a life category. These two functions
    have deliberately disjoint allow-lists.
    """

    def c(self, name):
        return classmap.resolve_category(name, CATEGORY_OPTIONS)

    def test_an_exact_category_is_accepted(self):
        for name in ("School", "Personal", "Friends", "Work"):
            self.assertEqual(self.c(name), name)

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(self.c("  Personal "), "Personal")

    def test_a_course_name_is_refused_even_if_it_contains_a_category(self):
        """
        The exact hazard the original ban existed for. Fuzzy matching is
        what filed real homework under a life category; this function
        does none, so "Personal Finance" is simply not a category.
        """
        for name in ("Personal Finance", "Work Experience", "School of Rock",
                     "Friends Seminar", "Personal Fitness", "Work Study"):
            self.assertIsNone(self.c(name), name)

    def test_a_real_class_name_is_refused(self):
        # Classes go through resolve(); the two allow-lists are disjoint.
        self.assertIsNone(self.c("AP Stats"))
        self.assertIsNone(self.c("AP Lang"))

    def test_an_invented_category_is_refused(self):
        # Notion silently CREATES any select option it is handed, so an
        # unrecognised value must never reach create_item.
        self.assertIsNone(self.c("Business"))
        self.assertIsNone(self.c("Family"))

    def test_case_must_match_the_live_option_exactly(self):
        # Notion matches select options on exact string; "personal" would
        # create a second, lowercase option next to the real one.
        self.assertIsNone(self.c("personal"))

    def test_a_category_missing_from_the_live_options_is_refused(self):
        """If Peter deletes or renames the option, we must not re-create it."""
        self.assertIsNone(classmap.resolve_category("Work", ["AP Lang", "Personal"]))

    def test_none_and_empty_are_refused(self):
        self.assertIsNone(self.c(None))
        self.assertIsNone(self.c(""))

    def test_the_two_resolvers_have_disjoint_ranges(self):
        """
        The invariant, stated directly: nothing resolve() can return is
        something resolve_category() can return, and vice versa. This is
        what makes "a course name can never become Personal" structural
        rather than a property of the current fuzzy thresholds.
        """
        for name in CATEGORY_OPTIONS:
            by_course = classmap.resolve(name, CATEGORY_OPTIONS)
            by_category = classmap.resolve_category(name, CATEGORY_OPTIONS)
            self.assertFalse(
                by_course is not None and by_category is not None,
                f"{name!r} is reachable through both resolvers",
            )


if __name__ == "__main__":
    unittest.main()
