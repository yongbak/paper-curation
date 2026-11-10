"""arXiv API 검색 — 카테고리 AND 주제 키워드 AND 제출일 범위.

arXiv 이용 규칙: 요청 간 3초 간격, 식별 가능한 User-Agent.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .config import Topic
from .net import CERT_HELP, ArxivUnavailable, CertificateProblem, is_certificate_error, ssl_context

API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "arxiv-review/1.0 (personal paper curation script)"
PAGE_SIZE = 100
REQUEST_INTERVAL = 3.0
ATOM = "{http://www.w3.org/2005/Atom}"
# 2611.01234 (신형) / hep-th/9901001, math.AG/0101001 (구형)
ARXIV_ID_RE = re.compile(r"^(\d{4}\.\d{4,5}|[a-z\-]+(\.[A-Z]{2})?/\d{7})$")
ARXIV = "{http://arxiv.org/schemas/atom}"


def build_query(topic: Topic, since: dt.date, until: dt.date) -> str:
    """`(cat:A OR cat:B) AND (ti:"k" OR abs:"k" ...) AND submittedDate:[..]`."""
    cats = " OR ".join(f"cat:{c}" for c in topic.categories)
    parts = [f"({cats})"]
    if topic.keywords:
        kws = " OR ".join(f'ti:"{k}" OR abs:"{k}"' for k in topic.keywords)
        parts.append(f"({kws})")
    parts.append(
        f"submittedDate:[{since:%Y%m%d}0000 TO {until:%Y%m%d}2359]")
    return " AND ".join(parts)


def _text(entry, tag: str) -> str:
    el = entry.find(tag)
    return " ".join((el.text or "").split()) if el is not None else ""


def parse_feed(xml_data: bytes) -> list[dict]:
    """Atom 응답 → 논문 dict 목록."""
    root = ET.fromstring(xml_data)
    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = _text(entry, f"{ATOM}id")
        if "/abs/" not in raw_id:
            continue  # 오류 entry
        versioned = raw_id.split("/abs/")[-1]
        arxiv_id = re.sub(r"v\d+$", "", versioned)
        primary = entry.find(f"{ARXIV}primary_category")
        papers.append({
            "arxiv_id": arxiv_id,
            "version": versioned[len(arxiv_id):],
            "title": _text(entry, f"{ATOM}title"),
            "abstract": _text(entry, f"{ATOM}summary"),
            "authors": [
                _text(a, f"{ATOM}name") for a in entry.findall(f"{ATOM}author")],
            "published": _text(entry, f"{ATOM}published")[:10],
            "updated": _text(entry, f"{ATOM}updated")[:10],
            "primary_category": primary.get("term", "") if primary is not None else "",
            "categories": [
                c.get("term", "") for c in entry.findall(f"{ATOM}category")],
            "comment": _text(entry, f"{ARXIV}comment"),
            "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        })
    return papers


def apply_filters(papers: list[dict], topic: Topic) -> list[dict]:
    """API 가 못 거르는 조건: primary 카테고리 한정, 제외 키워드, 중복."""
    seen, kept = set(), []
    excludes = [k.lower() for k in topic.exclude_keywords]
    for p in papers:
        if p["arxiv_id"] in seen:
            continue
        seen.add(p["arxiv_id"])
        if topic.primary_category_only and p["primary_category"] not in topic.categories:
            continue
        haystack = f"{p['title']} {p['abstract']}".lower()
        if any(k in haystack for k in excludes):
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
    """테스트용: 대기 상태와 캐시를 비운다."""
    global _cooldown_until
    with _state_lock:
        _cooldown_until = 0.0
        _cache.clear()


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


def page_size_for(topic: Topic, limit: int) -> int:
    """arXiv 에 요청할 편수. 필요한 만큼만 묻는다 (1편을 원하는데 100편을 묻지 않는다).

    limit+1 편이면 '기간 안에 더 있는지' 까지 알 수 있다. 제외 키워드·primary 한정은
    받은 뒤에 거르므로 여유를 둔다 — 너무 작게 물으면 페이지를 여러 번 요청하게 된다.
    """
    want = limit + 1
    if topic.exclude_keywords or topic.primary_category_only:
        want = want * 3
    return max(1, min(PAGE_SIZE, want))


def search_detailed(topic: Topic, *, since: dt.date | None = None,
                    until: dt.date | None = None, today: dt.date | None = None,
                    limit: int | None = None, fetch=_fetch) -> SearchResult:
    """최신 제출순으로 조건에 맞는 논문을 limit 편까지 모은다."""
    since, until = date_window(topic, since=since, until=until, today=today)
    limit = limit or topic.max_papers
    query = build_query(topic, since, until)
    page_size = page_size_for(topic, limit)
    collected: list[dict] = []
    start = 0
    while True:
        url = API_URL + "?" + urllib.parse.urlencode({
            "search_query": query, "start": start, "max_results": page_size,
            "sortBy": "submittedDate", "sortOrder": "descending"})
        page = parse_feed(fetch(url))
        collected = apply_filters(collected + page, topic)
        # limit 보다 1편 더 모아야 '잘렸는지' 알 수 있다
        if len(collected) > limit or len(page) < page_size:
            break
        start += page_size
    return SearchResult(papers=collected[:limit], since=since, until=until,
                        more_available=len(collected) > limit)


def search(topic: Topic, **kw) -> list[dict]:
    return search_detailed(topic, **kw).papers
