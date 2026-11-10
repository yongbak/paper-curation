"""arXiv API 검색 — 카테고리 AND 주제 키워드 AND 제출일 범위.

arXiv 이용 규칙: 요청 간 3초 간격, 식별 가능한 User-Agent.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .config import Topic

API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "arxiv-review/1.0 (personal paper curation script)"
PAGE_SIZE = 100
REQUEST_INTERVAL = 3.0
ATOM = "{http://www.w3.org/2005/Atom}"
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


def _fetch(url: str, retries: int = 3) -> bytes:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            wait = 10 * (attempt + 1)
            print(f"  arXiv 요청 실패({e}), {wait}초 후 재시도", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def search(topic: Topic, *, days: int | None = None, today: dt.date | None = None,
           limit: int | None = None, fetch=_fetch, sleep=time.sleep) -> list[dict]:
    """최신 제출순으로 조건에 맞는 논문을 limit 편까지 모은다."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    since = today - dt.timedelta(days=days or topic.days)
    limit = limit or topic.max_papers
    query = build_query(topic, since, today)
    collected: list[dict] = []
    start = 0
    while True:
        url = API_URL + "?" + urllib.parse.urlencode({
            "search_query": query, "start": start, "max_results": PAGE_SIZE,
            "sortBy": "submittedDate", "sortOrder": "descending"})
        page = parse_feed(fetch(url))
        collected = apply_filters(collected + page, topic)
        if len(collected) >= limit or len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE
        sleep(REQUEST_INTERVAL)
    return collected[:limit]
