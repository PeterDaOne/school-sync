"""
Tests for the launchd plist generator.

build_plist is pure (env dict in, XML string out) and was untested, which
matters more than it looks: this file BAKES EVERY SECRET IN .env INTO XML.
Two of its behaviours are security-relevant rather than cosmetic — it must
XML-escape values, and it must not emit a placeholder as if it were a real
credential.

It is also half of the local/cloud settings-parity guarantee; the other
half lives in tests/test_settings_parity.py.
"""

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import tests.context  # noqa: F401

import generate_plist
from shared import reminders


def env(**over):
    base = {
        "NOTION_TOKEN": "secret-token",
        "NOTION_DB_ID": "db-id",
        "GOOGLE_CLIENT_ID": "client-id",
        "GOOGLE_CLIENT_SECRET": "client-secret",
        "GOOGLE_REFRESH_TOKEN": "refresh-token",
        "GOOGLE_CALENDAR_ID": "primary",
        "NTFY_TOPIC": "topic",
    }
    base.update(over)
    return base


def build(**over):
    return generate_plist.build_plist(env(**over), "/usr/bin/python3", Path("/proj"))


def env_vars(xml: str) -> dict:
    """Pull the EnvironmentVariables dict back out of the generated XML."""
    root = ET.fromstring(xml)
    top = root.find("dict")
    children = list(top)
    for i, node in enumerate(children):
        if node.tag == "key" and node.text == "EnvironmentVariables":
            inner = list(children[i + 1])
            return {
                inner[j].text: inner[j + 1].text for j in range(0, len(inner), 2)
            }
    return {}


class BuildPlist(unittest.TestCase):
    def test_output_is_well_formed_xml(self):
        ET.fromstring(build())  # raises if not

    def test_required_values_are_carried(self):
        got = env_vars(build())
        self.assertEqual(got["NOTION_TOKEN"], "secret-token")
        self.assertEqual(got["NTFY_TOPIC"], "topic")

    def test_placeholders_are_omitted_not_written_as_real_values(self):
        got = env_vars(build(NTFY_SERVER="REPLACE_ME"))
        self.assertNotIn("NTFY_SERVER", got)

    def test_empty_values_are_omitted(self):
        got = env_vars(build(NTFY_COMMAND_TOPIC=""))
        self.assertNotIn("NTFY_COMMAND_TOPIC", got)

    def test_values_are_xml_escaped(self):
        """
        An unescaped & or < in a token would produce a plist launchd
        cannot parse -- the job would simply stop running, with the
        failure looking like "the Mac isn't syncing" rather than a
        config error.
        """
        xml = build(NOTION_TOKEN="a&b<c>d")
        ET.fromstring(xml)
        self.assertEqual(env_vars(xml)["NOTION_TOKEN"], "a&b<c>d")
        self.assertIn("&amp;", xml)

    def test_unset_optional_keys_are_simply_absent(self):
        got = env_vars(build())
        self.assertNotIn("QUIET_HOURS_START", got)

    def test_set_optional_keys_are_carried(self):
        got = env_vars(build(QUIET_HOURS_START="01:00"))
        self.assertEqual(got["QUIET_HOURS_START"], "01:00")

    def test_every_reminder_tunable_is_carriable(self):
        """
        The local half of the parity guarantee: setting any tunable in
        .env must reach the Mac. Five of them silently did not until
        2026-07-31 -- see tests/test_settings_parity.py.
        """
        extra = {k: "1" for k in reminders.TUNABLE_ENV_VARS}
        got = env_vars(generate_plist.build_plist(
            {**env(), **extra}, "/usr/bin/python3", Path("/proj")
        ))
        for key in reminders.TUNABLE_ENV_VARS:
            self.assertIn(key, got, f"{key} never reaches the launchd job")

    def test_cloud_only_settings_are_not_baked_in(self):
        """
        local_sync never reads these, and leaving them out keeps one
        fewer copy of a secret on disk.
        """
        got = env_vars(build(
            ANTHROPIC_API_KEY="sk-ant-real", SCHOOL_EMAIL_HINTS="school.example"
        ))
        self.assertNotIn("ANTHROPIC_API_KEY", got)
        self.assertNotIn("SCHOOL_EMAIL_HINTS", got)

    def test_the_interpreter_and_script_path_are_absolute(self):
        xml = build()
        self.assertIn("/usr/bin/python3", xml)
        self.assertIn("/proj/local_sync.py", xml)


if __name__ == "__main__":
    unittest.main()
