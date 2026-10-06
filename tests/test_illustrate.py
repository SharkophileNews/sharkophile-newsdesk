"""Adding or redoing the featured image on existing drafts."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from newsdesk.config import ROOT, load_config
from newsdesk.illustrate import illustrate_post

from .fakes import FakeHttp

ENV = {"ANTHROPIC_API_KEY": "sk-ant-test", "OPENAI_API_KEY": "sk-openai-test",
       "WP_USER": "Sharkophile Staff", "WP_APP_PASSWORD": "abcd efgh ijkl mnop"}

BODY = ("<!-- wp:paragraph -->\n<p>A kayaker was hurt in a Malibu shark bite on Sunday.</p>\n"
        "<!-- /wp:paragraph -->")


class IllustrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("config.yaml", "STYLE_GUIDE.md"):
            shutil.copy(ROOT / name, self.tmp / name)
        self.cfg = load_config(self.tmp / "config.yaml", env=ENV)
        self.http = FakeHttp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_post(self, **kw):
        post = {"id": 5833, "status": "draft", "slug": "malibu-shark-bite-kayaker-hooked-shark",
                "title": "Kayaker bitten twice by shark he hooked off Malibu", "content": BODY,
                "excerpt": "A kayaker fishing off Paradise Cove was bitten twice.", "featured_media": 0,
                "meta": {"_newsdesk_report": json.dumps({"warnings": [
                    "No featured image — add one before publishing. OpenAI said (429): no credits",
                    "New tags created: shortfin mako"], "seo": {"score": 80}})}}
        post.update(kw)
        self.http.wp.posts.append(post)
        return post

    def test_adds_image_to_old_draft_without_stored_prompt(self):
        post = self.add_post()
        r = illustrate_post(self.cfg, self.http, 5833)
        self.assertEqual(r["result"], "image added")
        self.assertEqual(post["featured_media"], r["media_id"])
        self.assertTrue(post["content"].startswith("<!-- wp:image"))
        self.assertIn(BODY, post["content"])
        self.assertIn("Illustration generated with AI for Sharkophile.", post["content"])
        media = self.http.wp.media[0]
        self.assertEqual(media["alt_text"], "A shortfin mako shark swims in deep blue open water")
        self.assertEqual(media.get("post"), 5833)
        self.assertIn("shortfin mako", self.http.openai_calls[0]["prompt"])
        self.assertIn("no people", self.http.openai_calls[0]["prompt"].lower())
        report = json.loads(post["meta"]["_newsdesk_report"])
        self.assertEqual(report["warnings"], ["New tags created: shortfin mako"])   # image warning cleared
        self.assertEqual(report["image_alt"], media["alt_text"])
        self.assertEqual(report["seo"], {"score": 80})                            # rest of report kept
        brief = [c for c in self.http.claude.calls if c["system"].startswith("You are Sharkophile's photo editor")]
        self.assertEqual(len(brief), 1)
        self.assertIn("Kayaker bitten twice", brief[0]["messages"][0]["content"])

    def test_uses_stored_prompt_without_calling_claude(self):
        self.add_post(meta={"_newsdesk_report": json.dumps({
            "image_prompt": "A blacktip shark over sand", "image_alt": "A blacktip shark over sand"})})
        illustrate_post(self.cfg, self.http, 5833)
        self.assertEqual(self.http.claude.calls, [])
        self.assertTrue(self.http.openai_calls[0]["prompt"].startswith("A blacktip shark over sand"))

    def test_skips_when_image_exists_unless_replace(self):
        post = self.add_post(featured_media=77, content="<!-- wp:image {\"id\":77} -->\n<figure>old</figure>\n"
                                                         "<!-- /wp:image -->\n\n" + BODY)
        r = illustrate_post(self.cfg, self.http, 5833)
        self.assertIn("already has a featured image", r["result"])
        self.assertEqual(self.http.openai_calls, [])
        r = illustrate_post(self.cfg, self.http, 5833, replace=True)
        self.assertEqual(r["result"], "image added")
        self.assertNotIn("<figure>old</figure>", post["content"])
        self.assertEqual(post["content"].count("<!-- wp:image"), 1)

    def test_never_touches_published_posts(self):
        self.add_post(status="publish")
        r = illustrate_post(self.cfg, self.http, 5833)
        self.assertIn("not a draft", r["result"])
        self.assertEqual(self.http.wp.updates, [])
        self.assertEqual(self.http.openai_calls, [])

    def test_image_failure_raises_and_leaves_post_alone(self):
        self.add_post()
        self.http.openai_no_credits = True
        with self.assertRaises(Exception) as ctx:
            illustrate_post(self.cfg, self.http, 5833)
        self.assertIn("no credits remaining", str(ctx.exception))
        self.assertEqual(self.http.wp.updates, [])

    def test_cli(self):
        from unittest import mock
        from newsdesk.__main__ import main
        self.add_post()
        with mock.patch("newsdesk.http.HttpClient", return_value=self.http), \
             mock.patch("newsdesk.__main__.load_config", return_value=self.cfg):
            code = main(["illustrate", "--post", "5833"])
        self.assertEqual(code, 0)
        self.assertTrue(self.http.wp.posts[0]["featured_media"])


if __name__ == "__main__":
    unittest.main()
