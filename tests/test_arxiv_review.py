import datetime as dt
import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from arxiv_review import arxiv, auth, cli, net, render, reviewer
from arxiv_review.fulltext import FullText
from arxiv_review.config import ConfigError, Topic, load_config

FIXTURES = Path(__file__).parent / "fixtures"
SETS = (FIXTURES / "oai_sets.xml").read_bytes()
PAGE1 = (FIXTURES / "oai_page1.xml").read_bytes()
PAGE2 = (FIXTURES / "oai_page2.xml").read_bytes()
EMPTY = (FIXTURES / "oai_empty.xml").read_bytes()
# 2611.01234, 2611.05678, 2605.20555(5월 논문의 11월 수정본), 2611.07777, hep-th/9901001
PAPERS = arxiv.parse_records(PAGE1)[0] + arxiv.parse_records(PAGE2)[0]


def fake_oai(urls=None):
    """URL 에 따라 분야 목록 / 1쪽 / 2쪽을 돌려주는 가짜 fetch."""
    def fetch(url):
        if urls is not None:
            urls.append(url)
        if "verb=ListSets" in url:
            return SETS
        return PAGE2 if "resumptionToken" in url else PAGE1
    return fetch
REVIEW = {k: f"{k} 에 대한 충분히 긴 한국어 서술입니다." for k in reviewer.TEXT_FIELDS}
REVIEW.update({k: 4 for k in reviewer.SCORE_FIELDS})
REVIEW["achievement"] = "1. **정확도**: 12% 향상\n2. **속도**: `2x` 개선"


def topic(**kw):
    base = dict(name="t", categories=["cs.CL", "cs.AI"], keywords=["LLM agent"])
    base.update(kw)
    return Topic(**base).validate()


class QueryTests(unittest.TestCase):
    def test_bad_category_and_quote_rejected(self):
        with self.assertRaises(ConfigError):
            topic(categories=["cs.CL OR all:x"])
        with self.assertRaises(ConfigError):
            topic(keywords=['a" OR all:"b'])
        with self.assertRaises(ConfigError):
            topic(categories=[])

    def test_category_to_oai_set(self):
        sets = arxiv.list_sets(lambda url: SETS)
        arxiv.reset_state()
        self.assertEqual(arxiv.set_for_category("cs.AI", sets), "cs:cs:AI")
        self.assertEqual(arxiv.set_for_category("quant-ph", sets), "physics:quant-ph")
        self.assertEqual(arxiv.set_for_category("cond-mat.mtrl-sci", sets), "physics:cond-mat:mtrl-sci")
        self.assertEqual(arxiv.set_for_category("stat.ML", sets), "stat:stat:ML")
        with self.assertRaises(ValueError) as cm:
            arxiv.set_for_category("cs.XX", sets)
        self.assertIn("arXiv 에 없는 분야", str(cm.exception))

    def test_phrase_matching_ignores_case_hyphen_plural(self):
        words = arxiv.tokens("Grounded Retrieval-Augmented Generation for LLM-Agents")
        self.assertTrue(arxiv.contains_phrase(words, "retrieval augmented generation"))
        self.assertTrue(arxiv.contains_phrase(words, "LLM agent"))
        self.assertTrue(arxiv.contains_phrase(words, "llm agents"))
        self.assertFalse(arxiv.contains_phrase(words, "agent llm"))       # 순서가 다르면 아님
        self.assertFalse(arxiv.contains_phrase(words, "augmented retrieval"))
        self.assertFalse(arxiv.contains_phrase(arxiv.tokens("ragged"), "RAG"))  # 단어 일부는 아님
        self.assertTrue(arxiv.contains_phrase(arxiv.tokens("Loss functions"), "loss"))  # loss 는 los 가 아님


class FeedTests(unittest.TestCase):
    def setUp(self):
        arxiv.reset_state()
        self.addCleanup(arxiv.reset_state)

    def test_parse_records(self):
        papers, token = arxiv.parse_records(PAGE1)
        self.assertEqual(token, "tok-page-2")
        self.assertEqual([p["arxiv_id"] for p in papers],
                         ["2611.01234", "2611.05678", "2605.20555"])  # 삭제된 기록은 빠진다
        p = papers[0]
        self.assertEqual(p["title"], "Tool-Using LLM Agents for Scientific Discovery")
        self.assertEqual(p["abstract"], "We study LLM agent tool use. Results improve by 12%.")
        self.assertEqual(p["authors"], ["Alice Kim", "Bob Lee"])
        self.assertEqual((p["published"], p["updated"], p["version"], p["primary_category"]),
                         ("2026-11-05", "2026-11-06", "v2", "cs.CL"))
        self.assertEqual(p["categories"], ["cs.CL", "cs.AI"])
        self.assertEqual(p["comment"], "12 pages")
        self.assertEqual(p["pdf_url"], "https://arxiv.org/pdf/2611.01234")
        # 수정본: 최근 버전은 11월이지만 처음 제출일(v1)은 5월
        self.assertEqual(papers[2]["published"], "2026-05-18")
        self.assertEqual(papers[2]["authors"], ["Dana Choi", "Eli Jung", "Fay Han"])
        self.assertEqual(arxiv.parse_records(PAGE2)[1], None)
        self.assertEqual(arxiv.parse_records(EMPTY), ([], None))
        with self.assertRaises(arxiv.ArxivError):
            arxiv.parse_records(b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
                                b'<error code="badArgument">bad</error></OAI-PMH>')

    def test_filters(self):
        t = topic()
        kept = arxiv.apply_filters(PAPERS + PAPERS, t)
        self.assertEqual([p["arxiv_id"] for p in kept],
                         ["2611.01234", "2611.05678", "2605.20555", "2611.07777"])  # 중복·분야 밖 제외
        only = arxiv.apply_filters(PAPERS, topic(primary_category_only=True))
        self.assertEqual([p["arxiv_id"] for p in only], ["2611.01234", "2605.20555", "2611.07777"])
        no_survey = arxiv.apply_filters(PAPERS, topic(exclude_keywords=["Surveys"]))
        self.assertNotIn("2611.05678", [p["arxiv_id"] for p in no_survey])
        dated = arxiv.apply_filters(PAPERS, t, dt.date(2026, 11, 1), dt.date(2026, 11, 6))
        self.assertEqual([p["arxiv_id"] for p in dated], ["2611.01234", "2611.05678"])
        rag = arxiv.apply_filters(PAPERS, topic(keywords=["retrieval-augmented generation"]))
        self.assertEqual([p["arxiv_id"] for p in rag], ["2611.07777"])
        everything = arxiv.apply_filters(PAPERS, topic(keywords=[]))
        self.assertEqual(len(everything), 4)

    def test_search_harvests_filters_and_sorts(self):
        urls = []
        result = arxiv.search_detailed(
            topic(max_papers=1), since=dt.date(2026, 11, 1), until=dt.date(2026, 11, 8),
            today=dt.date(2026, 11, 10), fetch=fake_oai(urls))
        self.assertEqual([p["arxiv_id"] for p in result.papers], ["2611.07777"])  # 최신 제출순
        self.assertTrue(result.more_available)
        self.assertEqual(result.oldest, "2026-11-07")
        self.assertEqual(result.scanned, 10)   # 두 분야 × 두 쪽 × 기록 5개 (중복 포함)
        self.assertIn("verb=ListSets", urls[0])
        first = urllib.parse.parse_qs(urllib.parse.urlparse(urls[1]).query)
        self.assertEqual(first["metadataPrefix"], ["arXivRaw"])
        self.assertEqual(first["set"], ["cs:cs:CL"])
        # 반영 지연만큼 뒤까지 받되 오늘을 넘지 않는다
        self.assertEqual((first["from"], first["until"]), (["2026-11-01"], ["2026-11-10"]))
        self.assertIn("resumptionToken=tok-page-2", urls[2])
        self.assertEqual(len(urls), 5)  # 분야 목록 1 + (분야 2개 × 2쪽)

    def test_search_full_window(self):
        result = arxiv.search_detailed(
            topic(max_papers=10), since=dt.date(2026, 11, 1), until=dt.date(2026, 11, 8),
            today=dt.date(2026, 11, 10), fetch=fake_oai())
        self.assertEqual([p["arxiv_id"] for p in result.papers],
                         ["2611.07777", "2611.01234", "2611.05678"])  # 5월 논문 수정본은 빠진다
        self.assertFalse(result.more_available)

    def test_unknown_category_is_reported(self):
        with self.assertRaises(ValueError):
            arxiv.search_detailed(topic(categories=["cs.XX"]), today=dt.date(2026, 11, 10),
                                  fetch=fake_oai())

    def test_explicit_date_range(self):
        with self.assertRaises(ValueError):
            arxiv.date_window(topic(), since=dt.date(2026, 11, 5), until=dt.date(2026, 11, 1))
        # until 만 주면 그날로부터 days 일 전
        self.assertEqual(arxiv.date_window(topic(days=3), until=dt.date(2026, 11, 5)),
                         (dt.date(2026, 11, 2), dt.date(2026, 11, 5)))

    def _limited(self, retry_after=None, calls=None):
        import io
        import urllib.error

        def urlopen(*a, **k):
            if calls is not None:
                calls.append(1)
            headers = {"Retry-After": str(retry_after)} if retry_after else {}
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", headers,
                                         io.BytesIO(b"Rate exceeded."))
        return urlopen

    def _patched(self, urlopen, waits):
        from contextlib import ExitStack
        from unittest import mock
        stack = ExitStack()
        stack.enter_context(mock.patch.object(arxiv.urllib.request, "urlopen", urlopen))
        stack.enter_context(mock.patch.object(arxiv.time, "sleep", waits.append))
        stack.enter_context(mock.patch.object(arxiv, "throttle", lambda: None))
        stack.callback(arxiv.reset_state)
        arxiv.reset_state()
        return stack

    def test_rate_limit_retries_then_explains(self):
        waits, calls = [], []
        with self._patched(self._limited(calls=calls), waits):
            with self.assertRaises(arxiv.RateLimited) as cm:
                arxiv._fetch("https://export.arxiv.org/api/query?a")
        self.assertEqual(len(calls), 3)
        self.assertEqual(waits, [30, 60])      # 일반 오류(10, 20초)보다 길게 기다린다
        self.assertEqual(cm.exception.retry_after, 90)
        self.assertIn("90초 뒤", str(cm.exception))
        self.assertIn('arXiv 응답: "Rate exceeded."', str(cm.exception))

    def test_retry_after_header_is_honored(self):
        waits = []
        with self._patched(self._limited(retry_after=45), waits):
            with self.assertRaises(arxiv.RateLimited):
                arxiv._fetch("https://export.arxiv.org/api/query?b")
        self.assertEqual(waits, [45, 45])

    def test_web_mode_fails_fast_and_holds_off(self):
        waits, calls = [], []
        with self._patched(self._limited(calls=calls), waits):
            for _ in range(3):  # 웹에서 검색 버튼을 세 번 눌러도
                with self.assertRaises(arxiv.RateLimited) as cm:
                    arxiv._fetch("https://export.arxiv.org/api/query?c", retries=1, wait_on_limit=False)
        self.assertEqual(len(calls), 1)       # arXiv 에는 한 번만 갔다 (나머지는 대기 중이라 보내지 않음)
        self.assertEqual(waits, [])
        self.assertEqual(cm.exception.retry_after, 30)

    def test_same_search_is_cached(self):
        import io
        from unittest import mock
        calls = []

        def ok(*a, **k):
            calls.append(1)
            return mock.MagicMock(__enter__=lambda s: io.BytesIO(PAGE1), __exit__=lambda *x: False)
        with self._patched(ok, []):
            first = arxiv._fetch("https://export.arxiv.org/api/query?d")
            second = arxiv._fetch("https://export.arxiv.org/api/query?d")
            arxiv._fetch("https://export.arxiv.org/api/query?e")
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 2)

    def test_certificate_error_is_explained_without_retry(self):
        import ssl
        import urllib.error
        from unittest import mock

        def bad_cert(*a, **k):
            raise urllib.error.URLError(ssl.SSLCertVerificationError(
                1, "certificate verify failed: unable to get local issuer certificate"))
        waits = []
        with mock.patch.object(arxiv.urllib.request, "urlopen", bad_cert), \
                mock.patch.object(arxiv.time, "sleep", waits.append), \
                mock.patch.object(arxiv, "throttle", lambda: None):
            with self.assertRaises(net.CertificateProblem) as cm:
                arxiv._fetch("https://export.arxiv.org/api/query")
        self.assertIn("pip install truststore", str(cm.exception))
        self.assertEqual(waits, [])  # 인증서 문제는 다시 시도해도 같다

    def test_trust_source_order(self):
        import builtins
        import sys
        from unittest import mock
        real_import = builtins.__import__

        def without(*names):
            def fake(name, *a, **k):
                if name in names:
                    raise ImportError(name)
                return real_import(name, *a, **k)
            return fake

        def source(env, missing=()):
            net._build.cache_clear()
            with mock.patch.dict("os.environ", env, clear=True), \
                    mock.patch.dict(sys.modules, {m: None for m in missing}), \
                    mock.patch.object(builtins, "__import__", without(*missing)):
                try:
                    return net.trust_source()
                finally:
                    net._build.cache_clear()
        self.assertIn("SSL_CERT_FILE", source({"SSL_CERT_FILE": "/x.pem"}))
        self.assertIn("기본", source({}, missing=("truststore", "certifi")))
        try:
            import certifi  # noqa: F401
            self.assertIn("certifi", source({}, missing=("truststore",)))
        except ImportError:
            pass

    def test_paper_dirname_rejects_paths(self):
        self.assertEqual(render.paper_dirname("hep-th/9901001"), "hep-th_9901001")
        self.assertEqual(render.paper_dirname("2611.01234"), "2611.01234")
        for bad in ("..", "../etc", "2611.01234/../x", ""):
            with self.assertRaises(ValueError):
                render.paper_dirname(bad)


class ReviewerTests(unittest.TestCase):
    paper = PAPERS[0]

    def fake_run(self, stdout, returncode=0, stderr=""):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
        return run, calls

    def test_uses_claude_cli_without_api_key(self):
        run, calls = self.fake_run(json.dumps({"is_error": False, "structured_output": REVIEW}))
        out = reviewer.review_paper(
            self.paper, FullText("full text body", 14), model="sonnet", run=run)
        self.assertEqual(out["overall"], 4)
        cmd, kw = calls[0]
        self.assertEqual(cmd[1], "-p")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("full text body", kw["input"])
        self.assertIn("Tool-Using LLM Agents", kw["input"])
        self.assertNotIn("ANTHROPIC_API_KEY", kw["env"])

    def test_unfinished_review_is_explained_briefly(self):
        envelope = {"is_error": True, "subtype": "error_max_structured_output_retries",
                    "stop_reason": "tool_use", "num_turns": 6, "usage": {"input_tokens": 8}}
        run, _ = self.fake_run(json.dumps(envelope))
        with self.assertRaises(reviewer.ReviewError) as cm:
            reviewer.review_paper(self.paper, None, run=run)
        self.assertIn("error_max_structured_output_retries", str(cm.exception))
        self.assertIn("sonnet", str(cm.exception))
        self.assertNotIn("usage", str(cm.exception))  # 응답 JSON 을 통째로 보여 주지 않는다

    def test_review_io_is_utf8_without_shell(self):
        """한국어 Windows 기본(CP949)이 아니라 UTF-8 로 주고받는다. 셸을 거치지 않는다."""
        run, calls = self.fake_run(json.dumps({"is_error": False, "structured_output": REVIEW}))
        paper = dict(self.paper, abstract="Bound ≤ ε with ∑ terms — naïve 🙂")
        reviewer.review_paper(paper, None, run=run)
        cmd, kw = calls[0]
        self.assertEqual((kw["encoding"], kw["errors"]), ("utf-8", "replace"))
        self.assertNotIn("shell", kw)
        self.assertIn("∑ terms", kw["input"])
        self.assertIn("--json-schema", cmd)

    def _shim(self, d, fixture, with_node=False, target=True):
        base = Path(d) / "npm"
        base.mkdir()
        shim = base / "claude.cmd"
        shim.write_bytes((FIXTURES / fixture).read_bytes())
        if target:
            js = base / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
            js.parent.mkdir(parents=True)
            js.write_text("// cli")
            (base / "claude.exe").write_text("MZ")
        if with_node:
            (base / "node.exe").write_text("MZ")
        return base, shim

    def test_windows_npm_shim_runs_node_directly(self):
        with tempfile.TemporaryDirectory() as d:
            base, shim = self._shim(d, "npm_claude.cmd")
            got = reviewer.resolve_cmd_shim(shim, which=lambda n: "C:/nodejs/node.exe")
            self.assertEqual(got, ["C:/nodejs/node.exe",
                                   str(base / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js")])
        with tempfile.TemporaryDirectory() as d:
            base, shim = self._shim(d, "npm_claude.cmd", with_node=True)  # npm 옆 node.exe 우선
            self.assertEqual(reviewer.resolve_cmd_shim(shim, which=lambda n: None)[0], str(base / "node.exe"))
        with tempfile.TemporaryDirectory() as d:
            base, shim = self._shim(d, "npm_native.cmd")
            self.assertEqual(reviewer.resolve_cmd_shim(shim), [str(base / "claude.exe")])

    def test_windows_shim_problems_are_explained(self):
        with tempfile.TemporaryDirectory() as d:
            _, shim = self._shim(d, "npm_claude.cmd", target=False)
            with self.assertRaises(reviewer.ReviewError):
                reviewer.resolve_cmd_shim(shim, which=lambda n: "node")
        with tempfile.TemporaryDirectory() as d:
            _, shim = self._shim(d, "npm_claude.cmd")
            with self.assertRaises(reviewer.ReviewError) as cm:
                reviewer.resolve_cmd_shim(shim, which=lambda n: None)  # node 없음
            self.assertIn("node", str(cm.exception))

    def test_claude_program_picks_the_cmd_on_windows(self):
        with tempfile.TemporaryDirectory() as d:
            base, shim = self._shim(d, "npm_claude.cmd")
            (base / "claude").write_text("#!/bin/sh")  # npm 이 함께 만드는 sh 스크립트
            for found in (str(shim), str(base / "claude")):
                got = reviewer.claude_program(
                    which=lambda n, f=found: f if n == "claude" else "node", windows=True)
                self.assertTrue(got[1].endswith("cli.js"), got)
            self.assertEqual(reviewer.claude_program(which=lambda n: "/usr/bin/claude", windows=False),
                             ["/usr/bin/claude"])
        with self.assertRaises(reviewer.ReviewError):
            reviewer.claude_program(which=lambda n: None)

    def test_subscription_env_strips_api_credentials(self):
        env = reviewer.subscription_env(
            {"ANTHROPIC_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": "y", "HOME": "/h"})
        self.assertEqual(env, {"HOME": "/h"})

    def test_abstract_only_prompt_says_so(self):
        self.assertIn("초록만 제공됨", reviewer.build_prompt(self.paper, None))

    def test_truncated_body_is_announced(self):
        whole = reviewer.build_prompt(self.paper, FullText("body", 4))
        self.assertIn("참고문헌 제외 전체", whole)
        cut = reviewer.build_prompt(self.paper, FullText("body", 46473))
        self.assertIn("전체 46,473자 중 앞 4자만", cut)
        self.assertIn("단정하지 말 것", cut)

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
        paper = PAPERS[0]
        meta = {"basis": "abstract", "model": "sonnet", "reviewed_at": "2026-11-10"}
        md = render.review_markdown(paper, REVIEW, meta)
        self.assertIn("# Tool-Using LLM Agents for Scientific Discovery", md)
        self.assertIn("> **리뷰 근거**: 초록", md)
        self.assertIn("| Overall | 4/5 |", md)
        self.assertIn("## 실험 결과", md)
        old = {k: v for k, v in REVIEW.items() if k != "experiments"}  # 이전 형식 리뷰
        self.assertNotIn("## 실험 결과", render.review_markdown(paper, old, meta))
        with tempfile.TemporaryDirectory() as d:
            pdir = Path(d) / "2611.01234"
            pdir.mkdir()
            (pdir / "paper.json").write_text(json.dumps(
                {"paper": paper, "review": REVIEW, "meta": meta}), encoding="utf-8")
            page = render.build_index(Path(d), "t", "desc").read_text(encoding="utf-8")
        self.assertIn("Tool-Using LLM Agents for Scientific Discovery", page)
        self.assertIn('<span id="n">1</span>', page)
        self.assertIn("<strong>정확도</strong>", page)


class NonUtf8LocaleTests(unittest.TestCase):
    """한국어 Windows 처럼 기본 인코딩이 UTF-8 이 아닌 Python 에서 실제 프로세스로 확인한다.
    (LC_ALL=C + -X utf8=0 → 기본 인코딩 ASCII. CP949 처럼 '∗'(U+2217)를 표현하지 못한다.)"""

    def run_child(self, code: str) -> subprocess.CompletedProcess:
        import os
        import sys
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONPATH=str(Path(__file__).parent.parent))
        env.pop("PYTHONIOENCODING", None)
        env.pop("PYTHONUTF8", None)
        return subprocess.run([sys.executable, "-X", "utf8=0", "-c", code],
                              capture_output=True, timeout=60, env=env)

    def test_paper_text_reaches_claude_as_utf8(self):
        # 사용자 기록: UnicodeEncodeError: 'cp949' codec can't encode character '\u2217'
        # → 표준 입력을 쓰던 스레드가 죽고 claude 는 "no stdin data received" 로 실패했다.
        proc = self.run_child(
            "import sys\n"
            "from unittest import mock\n"
            "from arxiv_review import reviewer\n"
            "echo = [sys.executable, '-c', 'import sys; sys.stdout.write(sys.stdin.buffer.read().hex())']\n"
            "with mock.patch.object(reviewer, 'claude_program', lambda: echo[:2]):\n"
            "    p = reviewer.run_claude([echo[2]], input='a \\u2217 b \\u2264 \\uc548', capture_output=True, timeout=30)\n"
            "print(p.stdout)\n")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
        self.assertEqual(bytes.fromhex(proc.stdout.decode().strip()).decode("utf-8"), "a ∗ b ≤ 안")

    def test_cli_output_survives_a_pipe(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"topics": {"math": {
                "categories": ["cs.AI"], "keywords": ["a ∗ b ≤ c"]}}}), encoding="utf-8")
            proc = self.run_child(
                "import sys; from arxiv_review.cli import main; "
                f"sys.exit(main(['--config', {str(cfg)!r}, 'topic', 'list']))")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
        self.assertIn("a ∗ b ≤ c", proc.stdout.decode("utf-8"))


class AuthTests(unittest.TestCase):
    def fake(self, status_json, status_text=""):
        def run(cmd, **kw):
            self.assertNotIn("ANTHROPIC_API_KEY", kw["env"])
            out = json.dumps(status_json) if cmd[-1] == "--json" else status_text
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return run

    def check(self, status_json, status_text=""):
        return auth.check(run=self.fake(status_json, status_text),
                          which=lambda name: "/bin/claude")

    def test_not_installed(self):
        status = auth.check(which=lambda name: None)
        self.assertIn("npm install", status.problem())

    def test_logged_out(self):
        status = self.check({"loggedIn": False})
        self.assertFalse(status.logged_in)
        self.assertIn("arxiv_review login", status.problem())

    def test_pro_subscription_by_login_method(self):
        status = self.check({"loggedIn": True, "authMethod": "claude.ai"},
                            "Login method: Claude Pro Account\nOrganization: x\n")
        self.assertEqual(status.method, "Claude Pro Account")
        self.assertIsNone(status.problem())

    def test_subscription_type_field(self):
        status = self.check({"loggedIn": True, "subscriptionType": "pro"}, "")
        self.assertIsNone(status.problem())

    def test_api_account_is_refused(self):
        status = self.check({"loggedIn": True, "authMethod": "oauth_token"},
                            "Login method: Claude API account\n")
        self.assertIn("구독으로 확인되지 않습니다", status.problem())
        self.assertIn("Claude API account", status.problem())


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

    def test_topic_add_list_remove(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "config.json")
            base = ["--config", path, "topic"]
            self.assertEqual(cli.main(base + [
                "add", "rag", "--category", "cs.CL", "--category", "cs.IR",
                "--keyword", "retrieval augmented generation", "--days", "3"]), 0)
            t = load_config(path).topics["rag"]
            self.assertEqual((t.categories, t.keywords, t.days, t.max_papers),
                             (["cs.CL", "cs.IR"], ["retrieval augmented generation"], 3, 10))
            # 같은 이름으로 다시 add 하면 준 값만 바뀐다
            self.assertEqual(cli.main(base + ["add", "rag", "--max", "5"]), 2)  # category 필수
            self.assertEqual(cli.main(base + ["add", "rag", "--category", "cs.CL", "--max", "5"]), 0)
            t = load_config(path).topics["rag"]
            self.assertEqual((t.categories, t.days, t.max_papers), (["cs.CL"], 3, 5))
            self.assertEqual(cli.main(base + ["add", "bad name", "--category", "cs.CL"]), 2)
            self.assertEqual(cli.main(base + ["remove", "rag"]), 0)
            self.assertEqual(load_config(path).topics, {})
            self.assertEqual(cli.main(base + ["remove", "rag"]), 2)

    def test_run_stops_before_review_without_subscription(self):
        original = auth.check
        auth.check = lambda: auth.LoginStatus(installed=True, logged_in=False)
        searched = []
        original_search = arxiv.search
        arxiv.search = lambda *a, **k: searched.append(1) or []
        try:
            code = cli.main(["--config", "/nonexistent.json", "run", "--category", "cs.CL"])
        finally:
            auth.check, arxiv.search = original, original_search
        self.assertEqual(code, 2)
        self.assertEqual(searched, [])

    def test_cli_run_reviews_then_skips(self):
        from arxiv_review import service
        saved = (arxiv.search_detailed, reviewer.review_paper)
        calls = []
        arxiv.search_detailed = lambda t, **kw: arxiv.SearchResult(
            PAPERS[:1], kw["since"], kw["until"], True)
        reviewer.review_paper = lambda *a, **k: calls.append(1) or dict(REVIEW)
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = Path(d) / "config.json"
                cfg.write_text(json.dumps({"output_dir": "out", "review": {"fulltext": False}}))
                argv = ["--config", str(cfg), "run", "--category", "cs.CL",
                        "--from", "2026-11-01", "--to", "2026-11-05", "--skip-login-check"]
                self.assertEqual(cli.main(argv), 0)
                self.assertEqual(cli.main(argv), 0)  # 두 번째는 건너뛴다
                self.assertEqual(len(calls), 1)
                entry = service.load_entry(Path(d) / "out", "adhoc", "2611.01234")
                self.assertEqual(entry["review"]["overall"], 4)
                self.assertTrue((Path(d) / "out" / "adhoc" / "index.html").exists())
                self.assertEqual(cli.main(argv[:-1] + ["--from", "2026-11-09", "--dry-run"]), 2)
        finally:
            arxiv.search_detailed, reviewer.review_paper = saved

    def test_skill_search_fulltext_save_flow(self):
        """skill 경로: search → fulltext → save. claude 호출 없이 같은 결과 파일이 나온다."""
        import contextlib
        import io
        from arxiv_review import fulltext, service
        saved = (arxiv.search_detailed, fulltext.fetch_fulltext, fulltext.pymupdf_available)
        arxiv.search_detailed = lambda t, **kw: arxiv.SearchResult(
            PAPERS[:1], dt.date(2026, 11, 1), dt.date(2026, 11, 5), True)
        body = "본문 첫 줄\n" + "실험 결과 " * 400
        seen = []
        fulltext.fetch_fulltext = lambda paper, max_chars: seen.append(max_chars) or FullText(
            text=body, total_chars=len(body))
        fulltext.pymupdf_available = lambda: True

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["--config", str(cfg), *argv])
            return code, out.getvalue()

        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = Path(d) / "config.json"
                cfg.write_text(json.dumps({"output_dir": "out"}))
                out_dir = Path(d) / "out"
                # 검색 전에는 fulltext/save 가 그 ID 를 받지 않는다
                self.assertEqual(run("fulltext", "2611.01234", "--topic", "rag")[0], 2)
                self.assertEqual(run("fulltext", "../etc", "--topic", "rag")[0], 2)

                code, out = run("search", "--topic", "rag", "--category", "cs.CL",
                                "--keyword", "RAG", "--from", "2026-11-01", "--to", "2026-11-05")
                found = json.loads(out)
                self.assertEqual(code, 0)
                self.assertEqual((found["topic"], found["since"], found["more_available"]),
                                 ("rag", "2026-11-01", True))
                paper = found["papers"][0]
                self.assertEqual((paper["arxiv_id"], paper["reviewed"]), ("2611.01234", False))
                self.assertTrue(paper["abstract"] and paper["authors"])

                code, out = run("fulltext", "2611.01234", "--topic", "rag")
                info = json.loads(out)
                self.assertEqual((code, info["chars"], info["lines"]), (0, len(body), 2))
                self.assertEqual(seen, [None])  # 자르지 않고 전체를 받는다
                self.assertEqual(Path(info["path"]).read_text(encoding="utf-8"), body)

                draft = Path(info["path"]).parent / "review.draft.json"
                save = ["save", "2611.01234", "--topic", "rag", "--review", str(draft)]
                draft.write_text(json.dumps({**REVIEW, "experiments": ""}), encoding="utf-8")
                self.assertEqual(run(*save, "--basis", "fulltext")[0], 2)  # 빈 필드 거부
                self.assertIsNone(service.load_entry(out_dir, "rag", "2611.01234"))
                draft.write_text(json.dumps(REVIEW, ensure_ascii=False), encoding="utf-8")
                self.assertEqual(run(*save, "--basis", "abstract", "--read-chars", "5")[0], 2)
                self.assertEqual(run(*save, "--basis", "fulltext",
                                     "--read-chars", str(len(body) + 1))[0], 2)
                code, out = run(*save, "--basis", "fulltext")
                self.assertEqual(code, 0)
                entry = service.load_entry(out_dir, "rag", "2611.01234")
                self.assertEqual(entry["meta"]["basis"], "fulltext")
                self.assertEqual(entry["meta"]["fulltext_chars"], len(body))
                self.assertEqual(entry["paper"]["title"], paper["title"])
                md = Path(json.loads(out)["saved"]).read_text(encoding="utf-8")
                self.assertIn("본문 전체", md)
                self.assertIn("## Abstract", md)
                page = (out_dir / "rag" / "index.html").read_text(encoding="utf-8")
                self.assertIn("<h3>Abstract</h3>", page)
                self.assertIn("주제 RAG", page)  # 등록 안 한 주제도 검색 조건이 남는다

                self.assertEqual(run(*save, "--basis", "fulltext")[0], 2)  # 이미 있음
                self.assertEqual(run(*save, "--basis", "fulltext", "--force",
                                     "--read-chars", "100")[0], 0)
                partial = service.load_entry(out_dir, "rag", "2611.01234")["meta"]
                self.assertEqual((partial["fulltext_chars"], partial["fulltext_total_chars"]),
                                 (100, len(body)))
                self.assertTrue(json.loads(run(
                    "search", "--topic", "rag", "--category", "cs.CL")[1])["papers"][0]["reviewed"])

                fulltext.pymupdf_available = lambda: False
                code, out = run("fulltext", "2611.01234", "--topic", "rag")
                self.assertEqual((code, json.loads(out)["path"]), (0, None))
        finally:
            arxiv.search_detailed, fulltext.fetch_fulltext, fulltext.pymupdf_available = saved

    def test_example_config_is_valid(self):
        config = load_config(Path(__file__).parent.parent / "config.example.json")
        self.assertIn("llm-agents", config.topics)


class WebTests(unittest.TestCase):
    """실제 HTTP 서버를 임의 포트에 띄워 API 와 보안 검사를 확인한다."""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from arxiv_review import service, web
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"output_dir": "out"}))
        self.patches = [(arxiv, "search_detailed", arxiv.search_detailed),
                        (auth, "check", auth.check),
                        (service, "review_and_save", service.review_and_save)]
        arxiv.search_detailed = lambda t, **kw: arxiv.SearchResult(
            arxiv.apply_filters(PAPERS, t)[:t.max_papers],
            kw.get("since") or dt.date(2026, 11, 3), kw.get("until") or dt.date(2026, 11, 10), False)
        auth.check = lambda: auth.LoginStatus(True, True, "Claude Pro Account", True)
        real_save = self.patches[2][2]

        def fake_review(paper, topic_name, output_dir, opts, log=print):
            from arxiv_review import reviewer as r
            original = r.review_paper
            r.review_paper = lambda *a, **k: dict(REVIEW)
            try:
                return real_save(paper, topic_name, output_dir,
                                 service.ReviewOptions(use_fulltext=False), log)
            finally:
                r.review_paper = original
        service.review_and_save = fake_review
        self.app = web.App(cfg)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = web.make_handler(self.app, self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for obj, name, value in self.patches:
            setattr(obj, name, value)
        self.tmp.cleanup()

    def call(self, path, body=None, headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": f"localhost:{self.port}"}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn.request("GET" if body is None else "POST", path,
                     None if body is None else json.dumps(body), hdrs)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        try:
            return res.status, json.loads(data)
        except ValueError:
            return res.status, data.decode()

    def test_page_and_status(self):
        status, page = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn("arXiv 한국어 리뷰", page)
        status, data = self.call("/api/status")
        self.assertTrue(data["login"]["subscription"])
        self.assertIsNone(data["login"]["problem"])

    def test_search_then_review_flow(self):
        status, data = self.call("/api/search", {
            "topic": "web", "categories": "cs.CL, cs.AI", "keywords": "LLM agent",
            "exclude": "survey", "date_from": "2026-11-01", "date_to": "2026-11-09", "max": 5})
        self.assertEqual(status, 200, data)
        self.assertEqual([r["paper"]["arxiv_id"] for r in data["results"]],
                         ["2611.01234", "2605.20555", "2611.07777"])
        self.assertEqual((data["since"], data["until"]), ("2026-11-01", "2026-11-09"))
        self.assertIsNone(data["results"][0]["review"])
        status, job = self.call("/api/review", {"topic": "web", "ids": ["2611.01234"]})
        self.assertEqual(status, 200, job)
        for _ in range(50):
            status, job = self.call(f"/api/jobs/{job['id']}")
            if job["state"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done")
        review = job["items"][0]["review"]
        self.assertIn("<strong>정확도</strong>", review["review_html"]["achievement"])
        self.assertIn("experiments", review["review_html"])
        out = Path(self.tmp.name) / "out" / "web"
        self.assertTrue((out / "2611.01234" / "paper.json").exists())
        self.assertTrue((out / "index.html").exists())  # CLI 와 같은 결과물
        # 다시 검색하면 저장된 리뷰가 함께 온다
        _, data = self.call("/api/search", {"topic": "web", "categories": "cs.CL",
                                            "keywords": "LLM agent", "max": 5})
        self.assertIsNotNone(data["results"][0]["review"])

    def test_client_disconnect_is_not_an_error(self):
        """응답 전에 브라우저가 끊어도(새로고침) 서버가 오류 응답을 또 쓰려다 실패하지 않는다."""
        import socket
        import struct
        errors = []
        self.server.handle_error = lambda request, address: errors.append(address)
        started = threading.Event()

        def slow_status():
            started.set()
            time.sleep(0.4)
            return {"ok": True}
        self.app.status = slow_status
        sock = socket.create_connection(("127.0.0.1", self.port))
        sock.sendall(f"GET /api/status HTTP/1.1\r\nHost: localhost:{self.port}\r\n\r\n".encode())
        started.wait(2)
        # RST 로 끊는다 — Windows 의 WinError 10053 처럼 서버의 쓰기가 실패하게
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
        time.sleep(0.8)
        self.assertEqual(errors, [])
        status, data = self.call("/api/topics")  # 서버는 계속 정상
        self.assertEqual(status, 200)

    def test_login_status_reuses_only_good_results(self):
        calls = []
        auth.check = lambda: calls.append(1) or auth.LoginStatus(True, True, "Claude Pro Account", True)
        self.call("/api/status")
        self.call("/api/status")
        self.assertEqual(len(calls), 1)
        self.app._login_cache = None
        auth.check = lambda: calls.append(1) or auth.LoginStatus(True, False)
        self.call("/api/status")
        self.call("/api/status")
        self.assertEqual(len(calls), 3)  # 로그인 안 된 상태는 매번 다시 확인

    def test_unknown_category_is_a_clear_400(self):
        def unknown(*a, **k):
            raise ValueError("arXiv 에 없는 분야입니다: 'cs.XX'")
        arxiv.search_detailed = unknown
        status, data = self.call("/api/search", {"categories": "cs.XX"})
        self.assertEqual(status, 400)
        self.assertIn("없는 분야", data["error"])

    def test_rate_limit_reports_wait_time(self):
        def limited(*a, **k):
            raise arxiv.RateLimited("arXiv 가 요청을 잠시 제한했습니다 (HTTP 429). 45초 뒤", retry_after=45)
        arxiv.search_detailed = limited
        status, data = self.call("/api/search", {"categories": "cs.CL"})
        self.assertEqual(status, 503)
        self.assertEqual(data["retry_after"], 45)
        self.assertIn("45초", data["error"])

    def test_review_only_for_searched_ids(self):
        status, data = self.call("/api/review", {"topic": "web", "ids": ["../../etc"]})
        self.assertEqual(status, 400)
        self.assertIn("검색으로 불러온 논문만", data["error"])

    def test_login_problem_blocks_review(self):
        self.call("/api/search", {"topic": "web", "categories": "cs.CL", "max": 5})
        auth.check = lambda: auth.LoginStatus(True, True, "Claude API account", False)
        status, data = self.call("/api/review", {"topic": "web", "ids": ["2611.01234"]})
        self.assertEqual(status, 409)
        self.assertTrue(data["login_problem"])

    def test_bad_input(self):
        status, data = self.call("/api/search", {"categories": "", "max": 5})
        self.assertEqual(status, 400)
        status, data = self.call("/api/search", {"categories": "cs.CL", "date_from": "11/01/2026"})
        self.assertEqual(status, 400)
        status, data = self.call("/api/search", {"categories": "cs.CL", "date_from": "2026-11-09",
                                                 "date_to": "2026-11-01"})
        self.assertEqual(status, 400)
        self.assertIn("시작일", data["error"])

    def test_topics_roundtrip(self):
        status, data = self.call("/api/topics", {"topic": "rag", "categories": "cs.CL cs.IR",
                                                 "keywords": "RAG\nretrieval", "max": 3, "days": 5})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["topics"]["rag"]["keywords"], ["RAG", "retrieval"])
        # 터미널 쪽 설정 파일에도 그대로 저장된다
        self.assertEqual(load_config(self.app.config_path).topics["rag"].days, 5)
        status, data = self.call("/api/topics/delete", {"topic": "rag"})
        self.assertEqual(data["topics"], {})

    def test_cross_site_requests_are_refused(self):
        status, _ = self.call("/api/status", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)
        status, _ = self.call("/api/search", {"categories": "cs.CL"},
                              headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.call("/api/search", {"categories": "cs.CL"},
                              headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)


if __name__ == "__main__":
    unittest.main()
