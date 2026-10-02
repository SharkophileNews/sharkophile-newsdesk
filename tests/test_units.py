import unittest
from datetime import datetime, timezone

from newsdesk import discover as d
from newsdesk import render, seo
from newsdesk.extract import extract_from_html
from newsdesk.state import State

from .fakes import FIXTURES, QUOTE


class FeedParsing(unittest.TestCase):
    def test_bing_rss_unwraps_and_canonicalizes(self):
        items = d.parse_feed((FIXTURES / "bing_shark.xml").read_text(), "Bing: shark")
        self.assertEqual(len(items), 7)
        first = items[0]
        self.assertEqual(first.url, "https://www.fixture-university.edu/news/shark-hearing")
        self.assertEqual(first.publisher, "Fixture University")
        self.assertEqual(first.published, datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(items[1].domain, "msn.com")

    def test_atom_feed(self):
        atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>Sawfish return to Florida Bay</title><link href="https://ex.org/sawfish?utm_medium=x"/>
        <updated>2026-09-30T08:00:00Z</updated><summary>&lt;p&gt;Smalltooth sawfish&lt;/p&gt;</summary></entry></feed>"""
        items = d.parse_feed(atom, "atom")
        self.assertEqual(items[0].url, "https://ex.org/sawfish")
        self.assertEqual(items[0].summary, "Smalltooth sawfish")

    def test_rss_guid_fallback_when_link_missing(self):
        rss = """<rss><channel><item><title>Shark tagged</title>
        <guid>https://ex.org/tagged</guid><pubDate>Wed, 30 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
        self.assertEqual(d.parse_feed(rss)[0].url, "https://ex.org/tagged")

    def test_bad_xml_returns_empty(self):
        self.assertEqual(d.parse_feed("<rss><channel><item>", "broken"), [])


class Filtering(unittest.TestCase):
    def setUp(self):
        self.items = d.parse_feed((FIXTURES / "bing_shark.xml").read_text(), "Bing: shark")
        from newsdesk.config import load_config
        self.cfg = load_config()

    def test_exclusions(self):
        pats = self.cfg["sources"]["exclude_patterns"]
        excluded = [i.title for i in self.items if d.is_excluded(i, pats)]
        self.assertIn("San Jose Sharks beat Kings 4-2 behind power play goals", excluded)
        self.assertIn("Shark Tank investors back a new kitchen gadget", excluded)
        self.assertEqual(len(excluded), 2)

    def test_keyword_match(self):
        it = d.Item(title="Sea spiders found", url="https://x.org", summary="deep sea")
        self.assertFalse(d.keyword_match(it, ["shark"]))
        it2 = d.Item(title="Megalodon teeth found in Maryland", url="https://x.org")
        self.assertTrue(d.keyword_match(it2, ["megalodon"]))

    def test_clustering_groups_same_story(self):
        hearing = [i for i in self.items if "hear" in i.title.lower()]
        others = [i for i in self.items if "flooded" in i.title.lower()]
        clusters = d.cluster_items(hearing + others)
        sizes = sorted(len(c.items) for c in clusters)
        self.assertEqual(sizes, [1, 3])


class Rendering(unittest.TestCase):
    def test_sanitizer_strips_unsafe_markup(self):
        out = render.sanitize_inline('Hi <script>alert(1)</script><a href="javascript:x()">bad</a> '
                                     '<a href="https://ok.org/a?b=1&c=2" onclick="x()">ok</a> <b>bold</b>')
        self.assertNotIn("<script", out)
        self.assertNotIn("javascript:", out)
        self.assertNotIn("onclick", out)
        self.assertIn('<a href="https://ok.org/a?b=1&amp;c=2">ok</a>', out)
        self.assertIn("<strong>bold</strong>", out)

    def test_markdown_links_become_html(self):
        self.assertIn('<a href="https://doi.org/1">paper</a>', render.sanitize_inline("the [paper](https://doi.org/1)"))

    def test_blocks(self):
        html = render.render_body([
            {"type": "paragraph", "text": "One.", "items": [], "attribution": ""},
            {"type": "heading", "text": "Why it matters", "items": [], "attribution": ""},
            {"type": "list", "text": "", "items": ["$500 fine", "seizure"], "attribution": ""},
            {"type": "quote", "text": "Quote here", "items": [], "attribution": "Dr. X"},
        ])
        self.assertEqual(html.count("<!-- wp:paragraph -->"), html.count("<!-- /wp:paragraph -->"))
        self.assertIn('<h2 class="wp-block-heading">Why it matters</h2>', html)
        self.assertIn("<!-- wp:list-item -->\n<li>$500 fine</li>", html)
        self.assertIn("<cite>Dr. X</cite>", html)

    def test_source_line(self):
        line = render.source_line([{"publisher": "UPI", "url": "https://upi.com/a", "role": "primary"},
                                   {"publisher": "PNAS", "url": "https://doi.org/1", "role": "study"}])
        self.assertIn("<em>Source:", line)
        self.assertIn("Study: <a href=\"https://doi.org/1\">PNAS</a>", line)


class SeoChecks(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(seo.slugify("The Most Dangerous Shark in 2019? The Diminutive Cookie Cutter!"),
                         "most-dangerous-shark-2019-diminutive-cookie-cutter")

    def test_trim_at_word(self):
        out = seo.trim_at_word("word " * 50, 155)
        self.assertLessEqual(len(out), 155)
        self.assertTrue(out.endswith("…"))

    def test_quote_verification(self):
        src = f'"{QUOTE}," said Dr. Mara Quill.'
        self.assertEqual(seo.unverified_quotes(f"“{QUOTE},” Quill said.", src), [])
        bad = seo.unverified_quotes("“Sharks are basically swimming ears and this changes everything,” she said.", src)
        self.assertEqual(len(bad), 1)

    def test_copy_detection(self):
        src = ("The team placed underwater speakers off the Florida coast and played recordings of "
               "struggling fish to thirty one tagged sharks over two winters in a row.")
        copied = seo.copied_passages("Notably the team placed underwater speakers off the Florida coast and "
                                     "played recordings of struggling fish to sharks.", src)
        self.assertTrue(copied)
        self.assertEqual(seo.copied_passages("Researchers used speakers to broadcast fish noises.", src), [])

    def test_title_case_detection(self):
        self.assertTrue(seo.looks_title_case("New Study Uncovers Spectacular Adaptations in Whale Shark Vision"))
        self.assertFalse(seo.looks_title_case("Hawaii bans shark fishing in all state waters"))

    def test_check_and_fix_trims_and_flags(self):
        from .fakes import good_draft
        dr = good_draft()
        dr["meta_description"] = "x " * 120 + " shark hearing study"
        dr["slug"] = "The Shark Hearing Study!!"
        rep = seo.check_and_fix(dr, f'"{QUOTE}," said Quill', "sharkophile.com", True)
        self.assertLessEqual(len(dr["meta_description"]), 155)
        self.assertEqual(dr["slug"], "shark-hearing-study")
        names = {c.name: c.ok for c in rep.checks}
        self.assertTrue(names["Quotes match sources word-for-word"])
        self.assertTrue(names["Internal link to related story"])
        self.assertTrue(names["Slug 3–7 words"])

    def test_banned_phrases(self):
        from .fakes import good_draft
        dr = good_draft()
        dr["body"][0]["text"] += " These majestic creatures delve deep."
        rep = seo.check_and_fix(dr, QUOTE, "sharkophile.com", False)
        banned = next(c for c in rep.checks if c.name == "No banned phrases")
        self.assertFalse(banned.ok)
        self.assertIn("majestic", banned.detail)


class Extraction(unittest.TestCase):
    def test_extract_article(self):
        html = (FIXTURES / "article_hearing.html").read_text()
        art = extract_from_html(html, "https://www.fixture-university.edu/news/shark-hearing")
        self.assertEqual(art.site_name, "Fixture University")
        self.assertIn(QUOTE, art.text)
        self.assertNotIn("Navigation paragraph", art.text)
        self.assertIn("https://doi.org/10.0000/fixture.2026.001", art.primary_links)


class StateTests(unittest.TestCase):
    def test_roundtrip_and_covered_match(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            s = State(path)
            now = datetime(2026, 10, 1, tzinfo=timezone.utc)
            s.mark_seen(["https://a.org/1"], now)
            s.add_covered("Sharks can hear sounds nearly 250 feet away", ["https://a.org/1"], 5, "H", now)
            s.save(now)
            s2 = State(path)
            self.assertTrue(s2.is_seen("https://a.org/1"))
            self.assertIsNotNone(s2.covered_match("Blacktip sharks can hear sounds 250 feet away, scientists say", []))
            self.assertIsNone(s2.covered_match("Hawaii bans shark fishing", ["https://b.org"]))


if __name__ == "__main__":
    unittest.main()


class SecretCleaning(unittest.TestCase):
    def test_forgives_paste_mistakes(self):
        from newsdesk.config import clean_secret
        self.assertEqual(clean_secret("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY\nsk-ant-abc123\n"), "sk-ant-abc123")
        self.assertEqual(clean_secret("OPENAI_API_KEY", 'OPENAI_API_KEY="sk-proj-xyz"'), "sk-proj-xyz")
        self.assertEqual(clean_secret("OPENAI_API_KEY", "export OPENAI_API_KEY=sk-proj-xyz"), "sk-proj-xyz")
        self.assertEqual(clean_secret("WP_APP_PASSWORD", "  abcd efgh ijkl mnop  "), "abcd efgh ijkl mnop")
        self.assertEqual(clean_secret("WP_USER", "Sharkophile News"), "Sharkophile News")
        self.assertIsNone(clean_secret("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"))
        self.assertIsNone(clean_secret("X", "   "))
