"""End-to-end runs of the newsdesk against fake services."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from newsdesk.config import ROOT, load_config
from newsdesk.pipeline import Newsdesk

from .fakes import FakeClaude, FakeHttp, FakeWordPress, bad_quote_draft, good_draft

NOW = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)

ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "OPENAI_API_KEY": "sk-openai-test",
    "WP_USER": "newsdesk",
    "WP_APP_PASSWORD": "abcd efgh ijkl mnop",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X",
}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("config.yaml", "STYLE_GUIDE.md"):
            shutil.copy(ROOT / name, self.tmp / name)
        self.cfg = load_config(self.tmp / "config.yaml", env=ENV)
        # The live config has alerts off; most tests exercise the Slack path.
        self.cfg.data["alerts"]["slack"] = True

    def test_alerts_off_sends_nothing(self):
        self.cfg.data["alerts"].update(slack=False, email=False, webhook=False)
        http = FakeHttp()
        summary = self.desk(http).run()
        self.assertEqual(len(summary["drafts"]), 1)
        self.assertEqual(summary["alerts_sent"], [])
        self.assertEqual(http.slack, [])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def desk(self, http, dry_run=False, now=NOW):
        return Newsdesk(self.cfg, http=http, dry_run=dry_run, now=now)

    # ------------------------------------------------------------------
    def test_full_run_creates_draft_and_alerts_editor(self):
        http = FakeHttp()
        summary = self.desk(http).run()

        self.assertNotIn("fatal", summary)
        self.assertEqual(len(summary["drafts"]), 1, summary["errors"])
        # flood story is skipped as too thin (fixture text < min_source_chars)
        self.assertTrue(any("not enough readable source text" in e for e in summary["errors"]))

        post = http.wp.posts[0]
        self.assertEqual(post["status"], "draft")
        self.assertEqual(post["author"], 2)
        self.assertEqual(post["slug"], "shark-hearing-study-blacktips-250-feet")
        self.assertEqual(post["categories"], [13, 4, 22])          # news, science, biology; Featured dropped
        self.assertIn(59, post["tags"])                              # existing "blacktip"
        self.assertNotIn(399, post["tags"])                          # legacy "#SharkResearch" tag not reused
        self.assertTrue(post["excerpt"].startswith("A new shark hearing study"))
        self.assertEqual(post["meta"]["_genesis_title"], post["meta"]["_genesis_title"][:60])
        self.assertIn("_genesis_description", post["meta"])
        self.assertIn("jetpack_publicize_message", post["meta"])
        report = json.loads(post["meta"]["_newsdesk_report"])
        self.assertEqual(report["fact_check"]["verdict"], "pass")

        content = post["content"]
        self.assertTrue(content.startswith("<!-- wp:image"))
        self.assertIn('src="https://www.sharkophile.com/wp-content/uploads/', content)  # image src filled
        self.assertIn(f'wp-image-{post["featured_media"]}', content)
        self.assertIn("Illustration generated with AI for Sharkophile.", content)
        self.assertIn("<em>Source:", content)
        self.assertNotIn("invented.example.com", content)           # hallucinated source filtered out
        self.assertNotIn("invented.example.com", post["meta"]["_newsdesk_sources"])

        media = http.wp.media[0]
        self.assertEqual(media["mime"], "image/jpeg")
        self.assertEqual(media["alt_text"], "Three blacktip sharks swim over a sandy seafloor in sunlit green water")
        prompt = http.openai_calls[0]["prompt"]
        self.assertIn("no people", prompt.lower())
        self.assertEqual(http.openai_calls[0]["model"], "gpt-image-2")

        # Blocked domains were never fetched as sources
        fetched = [u for m, u in http.log if m == "GET"]
        self.assertFalse(any("msn.com" in u or "dailymail" in u for u in fetched))

        # Editor alert
        self.assertEqual(summary["alerts_sent"], ["slack"])
        slack = json.dumps(http.slack[0])
        self.assertIn("wp-admin/post.php?post=", slack)
        self.assertIn("Blacktip sharks can hear prey sounds", slack)

        # State persisted
        state = json.loads((self.tmp / "state" / "newsdesk_state.json").read_text())
        self.assertEqual(len(state["covered"]), 1)

        # Model routing: triage on Haiku, writing on Opus, checks on Sonnet
        models = [c["model"] for c in http.claude.calls]
        self.assertEqual(models[0], "claude-haiku-4-5-20251001")
        self.assertIn("claude-opus-5-5", models)
        self.assertIn("claude-sonnet-5-5", models)
        writer = http.claude.writer_calls()[0]
        self.assertIn("output_config", writer)
        self.assertIn("HOUSE STYLE GUIDE", writer["system"])
        self.assertIn("blacktip-migration-florida", writer["messages"][0]["content"])

    def test_second_run_does_not_duplicate(self):
        http = FakeHttp()
        self.desk(http).run()
        http2 = FakeHttp(wp=http.wp)
        later = NOW.replace(hour=23)
        summary = self.desk(http2, now=later).run()
        self.assertEqual(summary["drafts"], [])
        self.assertEqual(len(http.wp.posts), 1)

    def test_fabricated_quote_triggers_revision(self):
        claude = FakeClaude()
        claude.writer_responses = [bad_quote_draft(), good_draft()]
        http = FakeHttp(claude=claude)
        summary = self.desk(http).run()
        self.assertEqual(len(claude.writer_calls()), 2)
        revise_prompt = claude.writer_calls()[1]["messages"][0]["content"]
        self.assertIn("Quotes match sources word-for-word", revise_prompt)
        d = summary["drafts"][0]
        self.assertTrue(d["revised"])
        self.assertFalse(any("Quotes match" in w for w in d["warnings"]))

    def test_unfixed_fact_check_problem_is_flagged_not_hidden(self):
        claude = FakeClaude()
        major = {"verdict": "major_issues", "summary": "Number wrong", "issues": [
            {"severity": "major", "excerpt": "31 tagged blacktips", "problem": "Source says 30", "fix": "Use 30"}]}
        claude.fact_responses = [major, major]
        http = FakeHttp(claude=claude)
        summary = self.desk(http).run()
        d = summary["drafts"][0]
        self.assertTrue(any("MAJOR" in w for w in d["warnings"]))
        self.assertIn("major issues", json.dumps(http.slack[0]).lower())

    def test_missing_mu_plugin_warns_and_still_drafts(self):
        http = FakeHttp(wp=FakeWordPress(meta_keys=["jetpack_publicize_message"]))
        summary = self.desk(http).run()
        post = http.wp.posts[0]
        self.assertNotIn("_genesis_title", post["meta"])
        self.assertTrue(post["excerpt"])
        self.assertTrue(any("Newsdesk Support plugin" in w for w in summary["drafts"][0]["warnings"]))

    def test_dry_run_writes_preview_only(self):
        http = FakeHttp()
        desk = self.desk(http, dry_run=True)
        summary = desk.run()
        self.assertEqual(http.wp.posts, [])
        self.assertEqual(http.wp.media, [])
        self.assertEqual(http.slack, [])
        self.assertEqual(len(summary["drafts"]), 1)
        preview = (desk.out / "shark-hearing-study-blacktips-250-feet.html").read_text()
        self.assertIn("Blacktip sharks can hear prey sounds", preview)
        self.assertTrue((desk.out / "shark-hearing-study-blacktips-250-feet.jpg").exists())
        self.assertFalse((self.tmp / "state" / "newsdesk_state.json").exists())

    def test_structured_output_fallback(self):
        claude = FakeClaude()
        claude.reject_structured = True
        http = FakeHttp(claude=claude)
        summary = self.desk(http).run()
        self.assertEqual(len(summary["drafts"]), 1)
        later = [c for c in claude.calls if "output_config" not in c]
        self.assertTrue(later and "JSON Schema" in later[0]["system"])

    def test_wordpress_auth_failure_alerts(self):
        http = FakeHttp()
        self.cfg.secrets.wp_app_password = None
        self.cfg.secrets.wp_user = None
        from newsdesk.pipeline import Newsdesk as ND
        summary = ND(self.cfg, http=http, now=NOW).run()
        self.assertIn("fatal", summary)
        self.assertEqual(summary["alerts_sent"], ["slack"])
        self.assertIn("failed", json.dumps(http.slack[0]).lower())

    def test_host_strips_authorization_but_plugin_fallback_works(self):
        http = FakeHttp(wp=FakeWordPress(strip_authorization=True, plugin_installed=True))
        summary = self.desk(http).run()
        self.assertNotIn("fatal", summary)
        self.assertEqual(len(http.wp.posts), 1)

    def test_host_strips_authorization_without_plugin_explains_fix(self):
        http = FakeHttp(wp=FakeWordPress(strip_authorization=True, plugin_installed=False))
        summary = self.desk(http).run()
        self.assertIn("stripping the Authorization header", summary["fatal"])
        self.assertIn("Sharkophile Newsdesk Support plugin", json.dumps(http.slack[0]))

    def test_editor_picked_url(self):
        http = FakeHttp()
        summary = self.desk(http).run(urls=["https://www.fixture-university.edu/news/shark-hearing"])
        self.assertEqual(len(summary["drafts"]), 1)
        self.assertFalse(any("bing.com" in u for _, u in http.log))

    def test_story_failure_still_alerts_and_is_retried_next_run(self):
        claude = FakeClaude()
        claude.writer_responses = [{"not": "a draft"}]   # malformed -> KeyError while checking
        http = FakeHttp(claude=claude)
        summary = self.desk(http).run()
        self.assertEqual(summary["drafts"], [])
        self.assertEqual(summary.get("failures"), 1)
        self.assertEqual(summary["alerts_sent"], ["slack"])
        # the failed story isn't marked seen, so the next run picks it up
        summary2 = self.desk(FakeHttp(wp=http.wp), now=NOW.replace(hour=23)).run()
        self.assertEqual(len(summary2["drafts"]), 1)

    def test_attribution_fallback_when_sources_filtered(self):
        claude = FakeClaude()
        d = good_draft()
        d["sources"] = [{"title": "x", "publisher": "Nowhere", "url": "https://invented.example.com/y",
                         "role": "primary"}]
        claude.writer_responses = [d]
        http = FakeHttp(claude=claude)
        self.desk(http).run()
        content = http.wp.posts[0]["content"]
        self.assertIn('<em>Source: <a href="https://www.fixture-university.edu/news/shark-hearing">', content)


if __name__ == "__main__":
    unittest.main()
