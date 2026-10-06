"""Planned features (content/calendar.yaml) and the Oct. 6 cost settings."""

import json
import re
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from newsdesk.config import ROOT, load_config
from newsdesk.features import FeatureDesk, due_entries, load_calendar
from newsdesk.pipeline import Newsdesk

from .fakes import FIXTURES, FakeClaude, FakeHttp

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)   # 8 a.m. Eastern, Tuesday Oct. 6
NEWS_NOW = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)  # matches the fixture feeds' dates
DOI = "https://doi.org/10.0000/fixture.2026.001"

ENV = {"ANTHROPIC_API_KEY": "sk-ant-test", "OPENAI_API_KEY": "sk-openai-test",
       "WP_USER": "newsdesk", "WP_APP_PASSWORD": "abcd efgh ijkl mnop"}

TEST_CALENDAR = """
features:
  - date: 2026-10-08
    pillar: sharkofiles
    title: "How sharks hear"
    focus_keyword: how sharks hear
    related: [blacktip]
    categories: [biology]
    brief: >
      A profile of shark hearing built on the new blacktip study.
  - date: 2026-10-13
    pillar: legends
    title: "Legendary sharks hub"
    hub: true
    related: [blacktip]
    brief: A hub page linking our legend stories.
  - date: 2026-10-15
    pillar: guide
    title: "Written by the editor"
    newsdesk: false
    brief: The editor writes this one.
"""


class FeatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("config.yaml", "STYLE_GUIDE.md"):
            shutil.copy(ROOT / name, self.tmp / name)
        (self.tmp / "content").mkdir()
        (self.tmp / "content" / "calendar.yaml").write_text(TEST_CALENDAR, encoding="utf-8")
        self.cfg = load_config(self.tmp / "config.yaml", env=ENV)
        # The fixture study page is short; real research pages run to many thousands of characters.
        self.cfg.data["features"]["min_material_chars"] = 600

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def http(self, with_research_page=True) -> FakeHttp:
        h = FakeHttp()
        if with_research_page:
            h.articles[DOI] = (FIXTURES / "article_hearing.html").read_text()
        return h

    def desk(self, http, dry_run=False, now=NOW) -> FeatureDesk:
        return FeatureDesk(self.cfg, http=http, dry_run=dry_run, now=now)

    # ------------------------------------------------------------------
    def test_due_window_and_done(self):
        entries = load_calendar(self.tmp / "content" / "calendar.yaml")
        due = due_entries(entries, date(2026, 10, 6), 2, lambda k: False)
        self.assertEqual([e.key for e in due], ["2026-10-08"])
        due = due_entries(entries, date(2026, 10, 11), 2, lambda k: False)
        self.assertEqual([e.key for e in due], ["2026-10-13"])
        self.assertEqual(due_entries(entries, date(2026, 10, 6), 2, lambda k: True), [])
        # editor-written entries are never drafted
        self.assertEqual(due_entries(entries, date(2026, 10, 14), 2, lambda k: False), [])

    def test_feature_run_creates_draft_once(self):
        http = self.http()
        summary = self.desk(http).run_features()
        self.assertNotIn("fatal", summary)
        self.assertEqual(len(summary["drafts"]), 1, summary["errors"])
        post = http.wp.posts[0]
        self.assertEqual(post["status"], "draft")
        self.assertEqual(sorted(post["categories"]), [4, 22])        # science, biology — not News
        self.assertIn("featured_media", post)
        self.assertIn('href="https://www.sharkophile.com/blacktip-migration-florida/"', post["content"])

        claude = http.claude
        self.assertEqual(claude.feature_writer_calls()[0]["model"], "claude-opus-5-5")
        self.assertEqual(claude.research_calls()[0]["tools"][0]["max_uses"], 5)
        report = json.loads(post["meta"]["_newsdesk_report"])
        self.assertTrue(any("Planned feature for Thursday, October 8, 2026" in w for w in report["warnings"]))
        self.assertIn("Length 700–1,600 words", [c["name"] for c in report["seo"]["checks"]])
        self.assertEqual(summary["drafts"][0]["planned_for"], "2026-10-08")

        # remembered: the next day's run doesn't draft it again
        state = json.loads(self.cfg.state_path.read_text())
        self.assertIn("2026-10-08", state["features"])
        again = self.desk(self.http(), now=datetime(2026, 10, 7, 11, 10, tzinfo=timezone.utc)).run_features()
        self.assertEqual(again["considered"], 0)
        self.assertEqual(again["drafts"], [])

    def test_specific_date_and_unknown_date(self):
        http = self.http()
        summary = self.desk(http).run_features(dates=["2026-10-13", "2026-12-25"])
        self.assertEqual(len(summary["drafts"]), 1)
        self.assertTrue(any("No calendar entry dated 2026-12-25" in e for e in summary["errors"]))

    def test_hub_reads_our_own_stories(self):
        http = self.http()
        self.desk(http).run_features(dates=["2026-10-13"])
        self.assertIn(("GET", "https://www.sharkophile.com/blacktip-migration-florida/"), http.log)

    def test_thin_research_is_skipped_not_invented(self):
        http = self.http(with_research_page=False)
        summary = self.desk(http).run_features()
        self.assertEqual(summary["drafts"], [])
        self.assertTrue(any("not enough research material" in e for e in summary["errors"]))
        self.assertEqual(http.wp.posts, [])
        self.assertEqual(http.claude.feature_writer_calls(), [])

    def test_dry_run_files_nothing(self):
        http = self.http()
        summary = self.desk(http, dry_run=True).run_features()
        self.assertEqual(len(summary["drafts"]), 1)
        self.assertEqual(http.wp.posts, [])
        self.assertFalse(self.cfg.state_path.exists())

    def test_nothing_due_makes_no_calls(self):
        http = self.http()
        summary = self.desk(http, now=datetime(2026, 12, 20, 12, tzinfo=timezone.utc)).run_features()
        self.assertEqual(summary["considered"], 0)
        self.assertEqual(http.claude.calls, [])
        self.assertFalse(any("wp-json" in u for _, u in http.log))


class LiveCalendarTest(unittest.TestCase):
    def test_live_calendar_matches_the_plan(self):
        entries = load_calendar(ROOT / "content" / "calendar.yaml")
        self.assertGreaterEqual(len(entries), 15)
        for e in entries:
            self.assertTrue(date(2026, 10, 7) <= e.date <= date(2026, 12, 5), e.key)
            self.assertIn(e.date.strftime("%A"), ("Tuesday", "Thursday"), e.key)
            text = (e.title + " " + e.brief).lower()
            self.assertNotIn("amazon", text, e.key)
            # "affiliate" may only appear as "no affiliate links"
            self.assertTrue(all(w == "no" for w in re.findall(r"(\w+) affiliate", text)), e.key)
        cfg = load_config(ROOT / "config.yaml", env={})
        allowed = set(cfg["wordpress"]["allowed_categories"])
        for e in entries:
            self.assertTrue(set(e.categories) <= allowed, e.key)


class CostSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("config.yaml", "STYLE_GUIDE.md"):
            shutil.copy(ROOT / name, self.tmp / name)
        self.cfg = load_config(self.tmp / "config.yaml", env=ENV)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_live_settings(self):
        cfg = load_config(ROOT / "config.yaml", env={})
        self.assertEqual(cfg["run"]["max_drafts_per_run"], 1)
        self.assertEqual(cfg["run"]["min_score"], 7)
        self.assertEqual(cfg["models"]["writer"], "claude-sonnet-5-5")
        self.assertEqual(cfg["models"]["feature_writer"], "claude-opus-5-5")
        wf = (ROOT / ".github" / "workflows" / "newsdesk.yml").read_text()
        self.assertEqual(len(re.findall(r"- cron:", wf)), 1)

    def test_news_written_on_sonnet_with_research_for_science(self):
        http = FakeHttp()
        summary = Newsdesk(self.cfg, http=http, now=NEWS_NOW).run()
        self.assertEqual(len(summary["drafts"]), 1, summary["errors"])
        self.assertEqual(http.claude.writer_calls()[0]["model"], "claude-sonnet-5-5")
        self.assertEqual(len(http.claude.research_calls()), 1)    # the hearing story is science

    def test_no_web_research_for_other_story_types(self):
        claude = FakeClaude()
        claude.triage_response = {"stories": [{
            "index": 0, "is_shark_story": True, "already_covered": False, "same_story_as": -1,
            "score": 8, "story_type": "odd", "focus_species": "", "search_terms": ["blacktip"],
            "reason": "test"}]}
        http = FakeHttp(claude=claude)
        Newsdesk(self.cfg, http=http, now=NEWS_NOW).run(urls=["https://www.fixture-university.edu/news/shark-hearing"])
        self.assertEqual(http.claude.research_calls(), [])
        self.assertEqual(len(http.wp.posts), 1)


if __name__ == "__main__":
    unittest.main()
