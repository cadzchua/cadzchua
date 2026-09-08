"""Offline regression tests for authentication, temporary failures and safe updates."""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("update_stats", Path(__file__).with_name("update_stats.py"))
stats = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stats)


def response(body):
    return io.BytesIO(json.dumps(body).encode() if not isinstance(body, bytes) else body)


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://api.github.com/test", code, "test error", headers or {}, None)


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(
            stats, TOKEN="workflow-token", PROFILE_TOKEN="profile-token", REJECTED_TOKENS=set(),
        ))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.sleep = self.stack.enter_context(patch.object(stats.time, "sleep"))
        self.urlopen = self.stack.enter_context(patch.object(stats.urllib.request, "urlopen"))

    def test_public_rest_does_not_use_optional_profile_token(self):
        self.urlopen.return_value = response({"public_repos": 12})
        self.assertEqual(stats.api("users/cadzchua"), {"public_repos": 12})
        self.assertEqual(self.urlopen.call_args.args[0].get_header("Authorization"), "Bearer workflow-token")

    def test_rejected_rest_token_is_not_retried_on_later_endpoints(self):
        self.urlopen.side_effect = [http_error(401), response({}), response([])]
        stats.api("users/cadzchua")
        stats.api("users/cadzchua/repos")
        headers = [call.args[0].get_header("Authorization") for call in self.urlopen.call_args_list]
        self.assertEqual(headers, ["Bearer workflow-token", None, None])
        self.sleep.assert_not_called()

    def test_expired_profile_token_falls_back_to_workflow_token(self):
        calendar = {"data": {"user": {"contributionsCollection": {
            "contributionCalendar": {"totalContributions": 1280},
        }}}}
        self.urlopen.side_effect = [http_error(401), response(calendar)]
        self.assertEqual(stats.contributions_trailing_year(), 1280)
        headers = [call.args[0].get_header("Authorization") for call in self.urlopen.call_args_list]
        self.assertEqual(headers, ["Bearer profile-token", "Bearer workflow-token"])
        self.assertIn("profile-token", stats.REJECTED_TOKENS)

    def test_graphql_errors_fall_back_to_public_calendar(self):
        self.urlopen.side_effect = [
            response({"errors": [{"message": "Resource not accessible by integration"}]}),
            response({"data": {"user": None}}),
            response(b'<tool-tip>No contributions on January 1st.</tool-tip>'),
        ]
        self.assertEqual(stats.contributions_trailing_year(), 0)
        self.assertIsNone(self.urlopen.call_args.args[0].get_header("Authorization"))

    def test_transient_server_and_network_errors_are_retried(self):
        self.urlopen.side_effect = [http_error(502), urllib.error.URLError("temporary timeout"), response({})]
        self.assertEqual(stats.api("users/cadzchua"), {})
        self.assertEqual(self.urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1, 2])

    def test_transient_retries_stop_after_three_attempts(self):
        self.urlopen.side_effect = http_error(503)
        with self.assertRaises(urllib.error.HTTPError):
            stats.api("users/cadzchua")
        self.assertEqual(self.urlopen.call_count, 3)

    def test_rate_limit_and_long_server_backoff_do_not_retry_early(self):
        for error in (http_error(429), http_error(403), http_error(503, {"Retry-After": "120"})):
            with self.subTest(status=error.code):
                self.urlopen.reset_mock()
                self.urlopen.side_effect = error
                with self.assertRaises(urllib.error.HTTPError):
                    stats.api("users/cadzchua")
                self.assertEqual(self.urlopen.call_count, 1)
        self.sleep.assert_not_called()

    def test_calendar_counts_nested_markup_and_explicit_zero_days(self):
        self.urlopen.return_value = response(b'''
            <tool-tip for="day-1"><strong>1,234</strong>&nbsp;contributions on January 1st.</tool-tip>
            <tool-tip for="day-2">1 contribution on January 2nd.</tool-tip>
            <tool-tip for="day-3">No contributions on January 3rd.</tool-tip>
        ''')
        self.assertEqual(stats.calendar_contributions(), 1235)

    def test_changed_calendar_markup_does_not_silently_return_zero(self):
        for markup in (b'<html>unavailable</html>', b'<tool-tip>Activity: 100</tool-tip>'):
            with self.subTest(markup=markup):
                self.urlopen.return_value = response(markup)
                with self.assertRaises(RuntimeError):
                    stats.calendar_contributions()

    def test_language_names_remain_valid_xml(self):
        legend = stats.render_legend(stats.language_slices({"A&B <script>": 42}))
        parsed = ET.fromstring(legend)
        self.assertEqual(parsed.find("g/text").text, "A&B <script>")

    def test_invalid_second_svg_preserves_both_existing_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "stats.svg", Path(directory) / "langs.svg"
            first.write_text("original stats", encoding="utf-8")
            second.write_text("original languages", encoding="utf-8")
            with self.assertRaises(ET.ParseError):
                stats.write_cards({first: "<svg/>", second: "<svg>"})
            self.assertEqual(first.read_text(encoding="utf-8"), "original stats")
            self.assertEqual(second.read_text(encoding="utf-8"), "original languages")
            self.assertEqual(len(list(Path(directory).iterdir())), 2)

    def test_refresh_renders_repository_templates_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "stats.svg", Path(directory) / "langs.svg"
            first.write_text(stats.STATS_SVG.read_text(encoding="utf-8"), encoding="utf-8")
            second.write_text(stats.LANGS_SVG.read_text(encoding="utf-8"), encoding="utf-8")
            data = {"contributions": 123, "repos": 10, "languages": 2, "prs": 4,
                    "byte_totals": {"Python": 700, "A&B": 300}}
            with patch.multiple(stats, STATS_SVG=first, LANGS_SVG=second), patch.object(stats, "collect", return_value=data):
                self.assertEqual(stats.main(), 0)
                self.assertEqual(ET.parse(first).find(".//*[@id='v-contrib']").text, "123")
                self.assertIn("A&amp;B", second.read_text(encoding="utf-8"))
                with patch.object(stats.os, "replace") as replace:
                    self.assertEqual(stats.main(), 0)
                    replace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
