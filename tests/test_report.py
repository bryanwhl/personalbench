import unittest
from html.parser import HTMLParser

from personalbench.report import render_report


class TagParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.remote = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.remote.extend(v for k, v in attrs if k in {"src", "href"} and v.startswith("http"))


class ReportTests(unittest.TestCase):
    def test_partial_coverage_labels_observed_scores_as_subset_only(self):
        rendered = render_report(
            {
                "summary": {
                    "coverage": {"scored_trials": 1, "expected_trials": 2},
                    "observed_success_pct": 100,
                    "observed_quality_pct": 90,
                }
            }
        )
        self.assertIn("Graded subset only: success 100.0%; output quality 90.0%", rendered)
        self.assertIn("do not establish an overall score", rendered)
        self.assertIn("Unavailable", rendered)

    def test_injection_escaped_everywhere_and_no_remote_assets(self):
        payload = '<script>alert("oops")</script><img src="https://example.com/x">'
        report = {
            "created_at": payload,
            "scope_note": payload,
            "configuration": {"name": payload, "harness": "fixture"},
            "summary": {"tasks": [{"task_id": payload, "category": payload}], "method": payload},
            "trials": [
                {
                    "task_id": payload,
                    "output": payload,
                    "grading": {"criteria": [{"id": payload, "evidence": payload}]},
                }
            ],
        }
        rendered = render_report(report)
        parser = TagParser()
        parser.feed(rendered)
        self.assertNotIn("script", parser.tags)
        self.assertNotIn("img", parser.tags)
        self.assertEqual(parser.remote, [])
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("Synthetic fixture", rendered)
        self.assertIn("Content-Security-Policy", rendered)
        self.assertIn("Unavailable", rendered)

    def test_recorded_label_and_empty_report(self):
        self.assertIn("Historical outcomes", render_report({"baseline_type": "recorded"}))
        result = render_report({})
        self.assertIn("No task evidence", result)
        self.assertIn("No attempts", result)
        self.assertIn('lang="en"', result)

    def test_task_manifest_escaped_and_recorded_fixture_not_synthetic(self):
        report = {
            "baseline_type": "recorded",
            "configuration": {"harness": "fixture"},
            "task_manifest": [
                {
                    "id": "research",
                    "brief": "Read <script>bad</script>",
                    "provenance": {"origin": "Synthetic source"},
                    "criteria": [{"description": "Check sources"}],
                }
            ],
        }
        rendered = render_report(report)
        self.assertIn("Task definitions", rendered)
        self.assertIn("Synthetic source", rendered)
        self.assertIn("Check sources", rendered)
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", rendered)
        self.assertNotIn("Synthetic fixture run", rendered)
        self.assertIn("Recorded baseline", rendered)
