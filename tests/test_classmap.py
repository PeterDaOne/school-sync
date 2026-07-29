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


class ClassEmoji(unittest.TestCase):
    def test_every_live_option_has_an_emoji(self):
        """
        Every canonical name Resolve can return should have an emoji, or
        a notification silently loses its visual identifier for that
        class with no error anywhere.
        """
        for option in OPTIONS:
            self.assertNotEqual(classmap.class_emoji(option), "", option)

    def test_unknown_class_returns_empty_not_a_placeholder(self):
        self.assertEqual(classmap.class_emoji("Marching Band"), "")

    def test_none_returns_empty(self):
        self.assertEqual(classmap.class_emoji(None), "")

    def test_misspelled_live_option_still_resolves(self):
        # The dict is keyed by the LIVE (misspelled) Notion option name
        # on purpose -- resolve() would hand this exact string back.
        self.assertEqual(classmap.class_emoji("AP Psycology"), "🧠")


if __name__ == "__main__":
    unittest.main()
