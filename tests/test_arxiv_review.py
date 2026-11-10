import datetime as dt
import json
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from arxiv_review import arxiv, cli, render, reviewer
from arxiv_review.config import ConfigError, Topic, load_config

FEED = (Path(__file__).parent / "fixtures" / "feed.xml").read_bytes()
REVIEW = {k: f"{k} 에 대한 충분히 긴 한국어 서술입니다." for k in reviewer.TEXT_FIELDS}
REVIEW.update({k: 4 for k in reviewer.SCORE_FIELDS})
REVIEW["achievement"] = "1. **정확도**: 12% 향상\n2. **속도**: `2x` 개선"


def topic(**kw):
    base = dict(name="t", categories=["cs.CL", "cs.AI"], keywords=["LLM agent"])
    base.update(kw)
    return Topic(**base).validate()


class QueryTests(unittest.TestCase):
    def test_query_combines_category_keyword_and_date(self):
        q = arxiv.build_query(topic(), dt.date(2026, 11, 3), dt.date(2026, 11, 10))
        self.assertEqual(
            q, '(cat:cs.CL OR cat:cs.AI) AND (ti:"LLM agent" OR abs:"LLM agent") '
               'AND submittedDate:[202611030000 TO 202611102359]')

    def test_no_keywords_means_whole_category(self):
        q = arxiv.build_query(topic(keywords=[]), dt.date(2026, 1, 1), dt.date(2026, 1, 2))
        self.assertNotIn("ti:", q)
        self.assertTrue(q.startswith("(cat:cs.CL OR cat:cs.AI) AND submittedDate"))

    def test_bad_category_and_quote_rejected(self):
        with self.assertRaises(ConfigError):
            topic(categories=["cs.CL OR all:x"])
        with self.assertRaises(ConfigError):
            topic(keywords=['a" OR all:"b'])
        with self.assertRaises(ConfigError):
            topic(categories=[])


class FeedTests(unittest.TestCase):
    def test_parse(self):
        papers = arxiv.parse_feed(FEED)
        self.assertEqual([p["arxiv_id"] for p in papers],
                         ["2611.01234", "2611.05678", "hep-th/9901001"])
        p = papers[0]
        self.assertEqual(p["title"], "Tool-Using LLM Agents for Scientific Discovery")
        self.assertEqual(p["abstract"], "We study LLM agent tool use. Results improve by 12%.")
        self.assertEqual(p["authors"], ["Alice Kim", "Bob Lee"])
        self.assertEqual((p["published"], p["version"], p["primary_category"]),
                         ("2026-11-05", "v2", "cs.CL"))
        self.assertEqual(p["categories"], ["cs.CL", "cs.AI"])
        self.assertEqual(p["pdf_url"], "https://arxiv.org/pdf/2611.01234")

    def test_filters(self):
        papers = arxiv.parse_feed(FEED)[:2]
        self.assertEqual(len(arxiv.apply_filters(papers + papers, topic())), 2)
        only = arxiv.apply_filters(papers, topic(primary_category_only=True))
        self.assertEqual([p["arxiv_id"] for p in only], ["2611.01234"])
        no_survey = arxiv.apply_filters(papers, topic(exclude_keywords=["Survey"]))
        self.assertEqual([p["arxiv_id"] for p in no_survey], ["2611.01234"])

    def test_search_requests_expected_url_and_limits(self):
        urls = []

        def fetch(url):
            urls.append(url)
            return FEED

        got = arxiv.search(topic(max_papers=1, days=7), today=dt.date(2026, 11, 10),
                           fetch=fetch, sleep=lambda s: None)
        self.assertEqual(len(got), 1)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(urls[0]).query)
        self.assertIn("submittedDate:[202611030000 TO 202611102359]",
                      params["search_query"][0])
        self.assertEqual(params["sortBy"], ["submittedDate"])
        self.assertEqual(len(urls), 1)  # 한 페이지가 꽉 차지 않으면 더 요청하지 않는다


class ReviewerTests(unittest.TestCase):
    paper = arxiv.parse_feed(FEED)[0]

    def fake_run(self, stdout, returncode=0, stderr=""):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
        return run, calls

    def test_uses_claude_cli_without_api_key(self):
        run, calls = self.fake_run(json.dumps({"is_error": False, "structured_output": REVIEW}))
        out = reviewer.review_paper(self.paper, "full text body", model="sonnet", run=run)
        self.assertEqual(out["overall"], 4)
        cmd, kw = calls[0]
        self.assertEqual(cmd[1], "-p")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("full text body", kw["input"])
        self.assertIn("Tool-Using LLM Agents", kw["input"])
        self.assertNotIn("ANTHROPIC_API_KEY", kw["env"])

    def test_subscription_env_strips_api_credentials(self):
        env = reviewer.subscription_env(
            {"ANTHROPIC_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": "y", "HOME": "/h"})
        self.assertEqual(env, {"HOME": "/h"})

    def test_abstract_only_prompt_says_so(self):
        self.assertIn("초록만 제공됨", reviewer.build_prompt(self.paper, None))

    def test_usage_limit_is_distinguished(self):
        run, _ = self.fake_run(json.dumps(
            {"is_error": True, "result": "Claude usage limit reached. Resets 3pm"}), 1)
        with self.assertRaises(reviewer.UsageLimitError):
            reviewer.review_paper(self.paper, None, run=run)

    def test_other_failure_and_incomplete_review(self):
        run, _ = self.fake_run("", 1, "not logged in")
        with self.assertRaises(reviewer.ReviewError) as cm:
            reviewer.review_paper(self.paper, None, run=run)
        self.assertNotIsInstance(cm.exception, reviewer.UsageLimitError)
        bad = dict(REVIEW, how="")
        run, _ = self.fake_run(json.dumps({"structured_output": bad}))
        with self.assertRaises(reviewer.ReviewError):
            reviewer.review_paper(self.paper, None, run=run)
        with self.assertRaises(reviewer.ReviewError):
            reviewer.validate_review(dict(REVIEW, overall=7))


class RenderTests(unittest.TestCase):
    def test_md_to_html(self):
        html_out = render.md_to_html("1. **A**: x\n2. b `c`\n\n- d <script>\n\n문단")
        self.assertIn("<ol>\n<li><strong>A</strong>: x</li>\n<li>b <code>c</code></li>\n</ol>",
                      html_out)
        self.assertIn("<li>d &lt;script&gt;</li>", html_out)
        self.assertIn("<p>문단</p>", html_out)

    def test_review_markdown_and_index(self):
        paper = arxiv.parse_feed(FEED)[0]
        meta = {"basis": "abstract", "model": "sonnet", "reviewed_at": "2026-11-10"}
        md = render.review_markdown(paper, REVIEW, meta)
        self.assertIn("# Tool-Using LLM Agents for Scientific Discovery", md)
        self.assertIn("> **리뷰 근거**: 초록", md)
        self.assertIn("| Overall | 4/5 |", md)
        with tempfile.TemporaryDirectory() as d:
            pdir = Path(d) / "2611.01234"
            pdir.mkdir()
            (pdir / "paper.json").write_text(json.dumps(
                {"paper": paper, "review": REVIEW, "meta": meta}), encoding="utf-8")
            page = render.build_index(Path(d), "t", "desc").read_text(encoding="utf-8")
        self.assertIn("Tool-Using LLM Agents for Scientific Discovery", page)
        self.assertIn('<span id="n">1</span>', page)
        self.assertIn("<strong>정확도</strong>", page)


class CliTests(unittest.TestCase):
    def args(self, **kw):
        base = dict(topic=None, category=None, keyword=None, exclude=None,
                    primary_only=False, days=None, max=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_config_topic_with_cli_override(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"topics": {"agents": {
                "categories": ["cs.CL"], "keywords": ["agent"], "days": 3}}}))
            config = load_config(path)
            self.assertEqual(config.output_dir, Path(d).resolve() / "output")
        t = cli.resolve_topic(self.args(topic="agents", max=2), config)
        self.assertEqual((t.categories, t.keywords, t.days, t.max_papers),
                         (["cs.CL"], ["agent"], 3, 2))

    def test_adhoc_and_errors(self):
        config = load_config(None)
        t = cli.resolve_topic(self.args(category=["cs.CV"], keyword=["diffusion"]), config)
        self.assertEqual((t.name, t.categories), ("adhoc", ["cs.CV"]))
        with self.assertRaises(ConfigError):
            cli.resolve_topic(self.args(topic="missing"), config)
        with self.assertRaises(ConfigError):
            cli.resolve_topic(self.args(), config)

    def test_example_config_is_valid(self):
        config = load_config(Path(__file__).parent.parent / "config.example.json")
        self.assertIn("llm-agents", config.topics)


if __name__ == "__main__":
    unittest.main()
