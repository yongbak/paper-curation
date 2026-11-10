"""arXiv 검색 — OAI-PMH 로 분야별 기록을 받아, 제출일·키워드는 직접 거른다.

arXiv 검색 API(export.arxiv.org/api/query)는 날짜 범위·키워드·여러 분야가 들어간 검색식을
네트워크에 따라 'Rate exceeded.'(429)로 거절했다. 단순한 `cat:cs.AI` 만 통과했다.
그래서 arXiv 가 메타데이터 수집용으로 제공하는 OAI-PMH 를 쓴다.

- 분야마다 `set=cs:cs:AI` 처럼 요청한다. 교차 등록(cs.LG 가 주 분야이고 cs.AI 도 단 논문)도 들어 있다.
- OAI 의 날짜(datestamp)는 기록이 바뀐 날이다 (새 논문 반영, 수정본 등록 모두). 그래서 arXivRaw
  형식의 버전 이력에서 v1 날짜를 처음 제출일로 쓰고, 그 날짜로 기간을 거른다. 반영은 제출보다
  1~3일 늦으므로 기간 끝에서 ANNOUNCE_LAG_DAYS 일 뒤까지 받는다.
- 키워드는 제목·초록에서 구문으로 찾는다. 대소문자·하이픈·단수/복수 차이는 무시한다.

arXiv 이용 규칙: 요청 간 3초 간격, 연결 하나씩, 식별 가능한 User-Agent.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from .config import Topic
from .net import CERT_HELP, ArxivUnavailable, CertificateProblem, is_certificate_error, ssl_context

OAI_URL = "https://oaipmh.arxiv.org/oai"
USER_AGENT = "arxiv-review/1.0 (personal paper curation script)"
REQUEST_INTERVAL = 3.0
ANNOUNCE_LAG_DAYS = 4      # 제출 → arXiv 반영까지 (주말 포함 최대 사흘 남짓)
MAX_PAGES_PER_SET = 50     # 안전장치: 한 분야에서 이 이상 페이지를 넘기지 않는다
# 2611.01234 (신형) / hep-th/9901001, math.AG/0101001 (구형)
ARXIV_ID_RE = re.compile(r"^(\d{4}\.\d{4,5}|[a-z\-]+(\.[A-Z]{2})?/\d{7})$")
OAI = "{http://www.openarchives.org/OAI/2.0/}"
RAW = "{http://arxiv.org/OAI/arXivRaw/}"


class ArxivError(ArxivUnavailable):
    """OAI-PMH 가 오류를 돌려줬다 (잘못된 인자 등)."""


def _text(el, tag: str) -> str:
    found = el.find(tag)
    return " ".join((found.text or "").split()) if found is not None else ""


def _version_date(version_el) -> str:
    """'Thu, 05 Nov 2026 07:34:20 GMT' → '2026-11-05' (UTC)."""
    raw = _text(version_el, f"{RAW}date")
    try:
        return parsedate_to_datetime(raw).astimezone(dt.timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return ""


def split_authors(raw: str) -> list[str]:
    """'A, B and C (MIT, USA)' → ['A', 'B', 'C']. 괄호 속 소속은 버린다."""
    depth, plain = 0, []
    for ch in raw:  # 괄호가 겹쳐도 바깥 괄호까지 지운다
        depth += ch == "("
        if depth == 0:
            plain.append(ch)
        depth -= ch == ")" and depth > 0
    names = re.split(r",\s*(?:and\s+)?|\s+and\s+", "".join(plain))
    return [" ".join(n.split()) for n in names if n.strip()]


def parse_records(xml_data: bytes) -> tuple[list[dict], str | None]:
    """ListRecords(arXivRaw) 응답 → (논문 dict 목록, 다음 페이지 resumptionToken).

    arXiv 형식의 <created> 는 '최근 버전' 날짜다 — 예전 논문의 수정본이 새 논문처럼 보인다.
    arXivRaw 는 버전마다 날짜가 있어 v1(처음 제출일)을 정확히 안다.
    """
    root = ET.fromstring(xml_data)
    error = root.find(f"{OAI}error")
    if error is not None:
        if error.get("code") == "noRecordsMatch":  # 그 기간에 반영된 기록이 없음
            return [], None
        raise ArxivError(f"arXiv OAI-PMH 오류 ({error.get('code')}): {(error.text or '').strip()}")
    papers = []
    for record in root.iter(f"{OAI}record"):
        header = record.find(f"{OAI}header")
        if header is not None and header.get("status") == "deleted":
            continue
        meta = record.find(f"{OAI}metadata/{RAW}arXivRaw")
        if meta is None:
            continue
        versions = meta.findall(f"{RAW}version")
        if not versions:
            continue
        arxiv_id = _text(meta, f"{RAW}id")
        categories = _text(meta, f"{RAW}categories").split()
        papers.append({
            "arxiv_id": arxiv_id,
            "version": versions[-1].get("version", ""),
            "title": _text(meta, f"{RAW}title"),
            "abstract": _text(meta, f"{RAW}abstract"),
            "authors": split_authors(_text(meta, f"{RAW}authors")),
            "published": _version_date(versions[0]),   # v1 제출일
            "updated": _version_date(versions[-1]),    # 최근 버전 제출일
            "primary_category": categories[0] if categories else "",
            "categories": categories,
            "comment": _text(meta, f"{RAW}comments"),
            "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        })
    token_el = root.find(f"{OAI}ListRecords/{OAI}resumptionToken")
    token = (token_el.text or "").strip() if token_el is not None else ""
    return papers, token or None


def _stem(word: str) -> str:
    # agents → agent, poisonings → poisoning. 'ss' 로 끝나는 말(loss, class)은 그대로.
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def tokens(text: str) -> list[str]:
    """비교용 단어 목록: 소문자, 악센트 제거, 영숫자만 (하이픈·구두점은 띄어쓰기로)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text)]


def contains_phrase(text_tokens: list[str], phrase: str) -> bool:
    """phrase 의 단어들이 같은 순서로 붙어서 나오는가. 'retrieval-augmented' = 'retrieval augmented'."""
    want = tokens(phrase)
    if not want:
        return False
    n = len(want)
    return any(text_tokens[i:i + n] == want for i in range(len(text_tokens) - n + 1))


def apply_filters(papers: list[dict], topic: Topic, since: dt.date | None = None,
                  until: dt.date | None = None) -> list[dict]:
    """분야·제출일·키워드·제외 키워드·중복. OAI 는 이 조건들을 서버에서 걸러 주지 않는다."""
    wanted_cats = set(topic.categories)
    seen, kept = set(), []
    for p in papers:
        if p["arxiv_id"] in seen:
            continue
        seen.add(p["arxiv_id"])
        cats = [p["primary_category"]] if topic.primary_category_only else p["categories"]
        if not wanted_cats.intersection(cats):
            continue
        if since and until and not (since.isoformat() <= p["published"] <= until.isoformat()):
            continue
        words = tokens(f"{p['title']} {p['abstract']}")
        if topic.keywords and not any(contains_phrase(words, k) for k in topic.keywords):
            continue
        if any(contains_phrase(words, k) for k in topic.exclude_keywords):
            continue
        kept.append(p)
    return kept


_throttle_lock = threading.Lock()
_last_request = 0.0


def throttle() -> None:
    """arXiv 로 가는 모든 요청(검색·PDF)을 프로세스 전체에서 3초 간격으로 묶는다.
    웹 서버에서는 검색과 리뷰 작업이 동시에 돌기 때문에 호출부마다 sleep 하면 안 된다."""
    global _last_request
    with _throttle_lock:
        wait = _last_request + REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


CACHE_TTL = 600          # 같은 검색은 10분 동안 다시 묻지 않는다
MAX_LIMIT_WAIT = 300     # Retry-After 가 아무리 길어도 이 이상은 기다리지 않는다
_state_lock = threading.Lock()
_cooldown_until = 0.0    # arXiv 가 429 를 준 뒤 다시 요청하지 않을 시각 (프로세스 전체)
_cache: dict[str, tuple[float, bytes]] = {}


def reset_state() -> None:
    """테스트용: 대기 상태, 응답 캐시, 분야 목록을 비운다."""
    global _cooldown_until, _sets
    with _state_lock:
        _cooldown_until = 0.0
        _cache.clear()
    _sets = None


def _cooldown_remaining() -> int:
    with _state_lock:
        return max(0, math.ceil(_cooldown_until - time.monotonic()))


def _start_cooldown(seconds: int) -> None:
    global _cooldown_until
    with _state_lock:
        _cooldown_until = max(_cooldown_until, time.monotonic() + seconds)


def _retry_after(e: urllib.error.HTTPError) -> int | None:
    value = (e.headers or {}).get("Retry-After") if hasattr(e, "headers") else None
    try:
        return max(1, int(str(value).strip()))
    except (TypeError, ValueError):
        return None  # HTTP 날짜 형식이거나 없음 → 기본 간격 사용


def _response_text(e: urllib.error.HTTPError) -> str:
    """arXiv 가 오류와 함께 보낸 본문 (예: 'Rate exceeded.'). 진단용으로 앞부분만."""
    try:
        return " ".join(e.read(300).decode("utf-8", "replace").split())
    except Exception:
        return ""


def _limit_message(seconds: int, detail: str = "") -> str:
    said = f" arXiv 응답: \"{detail}\"" if detail else ""
    return (f"arXiv 가 요청을 잠시 제한했습니다 (HTTP 429).{said} {seconds}초 뒤 다시 시도하세요.\n"
            "  이 컴퓨터에서 검색을 많이 하지 않았다면, 같은 네트워크(회사·기관 등)를 쓰는 "
            "다른 사용자의 요청이 함께 집계됐을 수 있습니다.")


def _fetch(url: str, retries: int = 3, wait_on_limit: bool = True) -> bytes:
    """arXiv 요청. wait_on_limit=False 면 제한(429) 중일 때 기다리지 않고 바로 RateLimited."""
    with _state_lock:
        hit = _cache.get(url)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    for attempt in range(retries):
        remaining = _cooldown_remaining()
        if remaining:
            if not wait_on_limit:
                raise RateLimited(_limit_message(remaining), retry_after=remaining)
            print(f"  arXiv 요청 제한 중 — {remaining}초 기다립니다", file=sys.stderr)
            time.sleep(remaining)
        try:
            throttle()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60, context=ssl_context()) as resp:
                data = resp.read()
            with _state_lock:
                _cache[url] = (time.monotonic(), data)
            return data
        except (urllib.error.URLError, TimeoutError) as e:
            if is_certificate_error(e):  # 다시 시도해도 같다 — 바로 해결 방법을 보여 준다
                raise CertificateProblem(CERT_HELP) from e
            if isinstance(e, urllib.error.HTTPError) and e.code in (429, 503):
                delay = min(_retry_after(e) or 30 * (attempt + 1), MAX_LIMIT_WAIT)
                _start_cooldown(delay)
                detail = _response_text(e)
                print(f"  arXiv HTTP {e.code} (Retry-After: {e.headers.get('Retry-After') if e.headers else None}, "
                      f"응답: {detail!r}) — {delay}초 동안 요청하지 않습니다", file=sys.stderr)
                if attempt == retries - 1 or not wait_on_limit:
                    raise RateLimited(_limit_message(delay, detail), retry_after=delay) from e
                continue  # 다음 시도 앞에서 cooldown 만큼 기다린다
            if attempt == retries - 1:
                raise
            wait = 10 * (attempt + 1)
            print(f"  arXiv 요청 실패({e}), {wait}초 후 재시도", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


class RateLimited(ArxivUnavailable):
    """arXiv 가 요청을 일시 제한했다 (HTTP 429/503)."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class SearchResult:
    papers: list[dict]
    since: dt.date
    until: dt.date
    more_available: bool   # max_papers 때문에 기간 안의 논문이 잘렸다
    scanned: int = 0       # arXiv 에서 받아 살펴본 기록 수 (거르기 전)

    @property
    def oldest(self) -> str:
        return self.papers[-1]["published"] if self.papers else ""


def date_window(topic: Topic, *, since: dt.date | None = None,
                until: dt.date | None = None, today: dt.date | None = None):
    """since/until 을 직접 주면 그대로, 아니면 '오늘에서 topic.days 일 전 ~ 오늘'."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    until = until or today
    since = since or (until - dt.timedelta(days=topic.days))
    if since > until:
        raise ValueError(f"시작일({since})이 종료일({until})보다 늦습니다")
    return since, until


_sets: list[str] | None = None


def list_sets(fetch) -> list[str]:
    """OAI-PMH 의 분야 묶음 이름 (예: cs:cs:AI, physics:quant-ph). 프로세스에서 한 번만 받는다."""
    global _sets
    if _sets is None:
        root = ET.fromstring(fetch(OAI_URL + "?verb=ListSets"))
        _sets = [(el.text or "").strip() for el in root.iter(f"{OAI}setSpec")]
    return _sets


def set_for_category(category: str, sets: list[str]) -> str:
    """cs.AI → cs:cs:AI, quant-ph → physics:quant-ph, cond-mat.mtrl-sci → physics:cond-mat:mtrl-sci."""
    suffix = category.replace(".", ":")
    for spec in sets:
        if spec == suffix or spec.endswith(":" + suffix):
            return spec
    raise ValueError(
        f"arXiv 에 없는 분야입니다: {category!r}. 목록: https://arxiv.org/category_taxonomy")


def harvest(set_spec: str, frm: dt.date, until: dt.date, fetch) -> list[dict]:
    """한 분야에서 [frm, until] 에 arXiv 에 반영된 기록을 모두 받는다 (페이지 넘김 포함)."""
    url = OAI_URL + "?" + urllib.parse.urlencode({
        "verb": "ListRecords", "metadataPrefix": "arXivRaw", "set": set_spec,
        "from": frm.isoformat(), "until": until.isoformat()})
    papers: list[dict] = []
    for _ in range(MAX_PAGES_PER_SET):
        page, token = parse_records(fetch(url))
        papers.extend(page)
        if not token:
            return papers
        url = OAI_URL + "?" + urllib.parse.urlencode({"verb": "ListRecords", "resumptionToken": token})
    print(f"  경고: {set_spec} 기록이 너무 많아 {MAX_PAGES_PER_SET} 페이지에서 멈췄습니다. "
          "기간을 줄이세요.", file=sys.stderr)
    return papers


def search_detailed(topic: Topic, *, since: dt.date | None = None,
                    until: dt.date | None = None, today: dt.date | None = None,
                    limit: int | None = None, fetch=_fetch, log=None) -> SearchResult:
    """기간 안에 처음 제출된, 조건에 맞는 논문을 최신순으로 limit 편까지."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    since, until = date_window(topic, since=since, until=until, today=today)
    limit = limit or topic.max_papers
    sets = list_sets(fetch)
    specs = list(dict.fromkeys(set_for_category(c, sets) for c in topic.categories))
    harvest_until = min(until + dt.timedelta(days=ANNOUNCE_LAG_DAYS), today)
    collected: list[dict] = []
    for spec in specs:
        if log:
            log(f"  {spec} 기록 받는 중 ({since} ~ {harvest_until} 반영분)")
        collected.extend(harvest(spec, since, harvest_until, fetch))
    papers = apply_filters(collected, topic, since, until)
    papers.sort(key=lambda p: (p["published"], p["arxiv_id"]), reverse=True)
    return SearchResult(papers=papers[:limit], since=since, until=until,
                        more_available=len(papers) > limit, scanned=len(collected))


def search(topic: Topic, **kw) -> list[dict]:
    return search_detailed(topic, **kw).papers
