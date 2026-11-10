"""CLI 와 웹 서버가 함께 쓰는 핵심 동작: 논문 1편 리뷰 → 저장, 저장된 리뷰 읽기.

두 진입점이 같은 함수를 부르므로 결과 파일(output/<주제>/<id>/...)도 같다.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from . import fulltext, render, reviewer
from .config import Topic


@dataclass
class ReviewOptions:
    model: str = "sonnet"
    use_fulltext: bool = True
    max_chars: int = 120000
    timeout: int = 600


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def entry_path(output_dir: Path, topic_name: str, arxiv_id: str) -> Path:
    return output_dir / topic_name / render.paper_dirname(arxiv_id) / "paper.json"


def load_entry(output_dir: Path, topic_name: str, arxiv_id: str) -> dict | None:
    """리뷰가 끝난 논문의 {paper, review, meta}. 없으면 None."""
    path = entry_path(output_dir, topic_name, arxiv_id)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return entry if entry.get("review") else None


def review_and_save(paper: dict, topic_name: str, output_dir: Path,
                    opts: ReviewOptions, log=print) -> dict:
    """논문 1편을 리뷰해 review.md + paper.json 으로 저장하고 entry 를 돌려준다.

    실패하면 reviewer.ReviewError / UsageLimitError 를 그대로 올린다 (아무것도 쓰지 않음).
    """
    text = None
    if opts.use_fulltext:
        text = fulltext.fetch_fulltext(paper, opts.max_chars)
        if text and text.truncated:
            log(f"  본문 {text.total_chars:,}자 중 앞 {len(text.text):,}자만 사용 "
                "(config.json 의 review.max_chars 로 조정)")
    review = reviewer.review_paper(paper, text, model=opts.model, timeout=opts.timeout)
    return save_review(
        paper, review, topic_name, output_dir, model=opts.model,
        fulltext_chars=len(text.text) if text else None,
        fulltext_total_chars=text.total_chars if text else None)


def save_review(paper: dict, review: dict, topic_name: str, output_dir: Path, *,
                model: str, fulltext_chars: int | None = None,
                fulltext_total_chars: int | None = None) -> dict:
    """검증된 리뷰를 review.md + paper.json 으로 저장한다.

    `claude -p` 경로(review_and_save)와 skill 경로(cli save)가 함께 쓴다 —
    저장 형식이 한 곳에 있어야 두 경로의 결과물이 같은 index.html 에 섞인다.
    fulltext_chars 가 None 이면 초록 기반.
    """
    meta = {
        "basis": "fulltext" if fulltext_chars else "abstract",
        "model": model,
        "topic": topic_name,
        "reviewed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if fulltext_chars:
        meta["fulltext_chars"] = fulltext_chars
        meta["fulltext_total_chars"] = fulltext_total_chars or fulltext_chars
    entry = {"paper": paper, "review": review, "meta": meta}
    record = entry_path(output_dir, topic_name, paper["arxiv_id"])
    record.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(record.parent / "review.md", render.review_markdown(paper, review, meta))
    # paper.json 을 마지막에 쓴다 — 이 파일의 존재가 '리뷰 완료' 표시다.
    _atomic_write(record, json.dumps(entry, ensure_ascii=False, indent=2))
    return entry


# ── skill 경로: 검색 결과 캐시, 본문 파일 ─────────────────────────────────────
# `search` 가 받은 논문 메타데이터를 남겨 두고 `fulltext` / `save` 가 arXiv ID 로 찾는다.
# 메타데이터를 명령행으로 다시 받지 않으므로, 검색으로 확인된 논문만 저장된다.

def _search_cache(output_dir: Path, topic_name: str) -> Path:
    return output_dir / topic_name / "search.json"


def remember_search(output_dir: Path, topic_name: str, papers: list[dict],
                    description: str) -> None:
    path = _search_cache(output_dir, topic_name)
    try:
        known = json.loads(path.read_text(encoding="utf-8")).get("papers", {})
    except (OSError, json.JSONDecodeError):
        known = {}
    known.update({p["arxiv_id"]: p for p in papers})
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps({"description": description, "papers": known},
                                   ensure_ascii=False, indent=2))


def searched_paper(output_dir: Path, topic_name: str, arxiv_id: str) -> dict | None:
    """검색 캐시 → 없으면 이미 저장된 리뷰의 메타데이터."""
    try:
        cache = json.loads(_search_cache(output_dir, topic_name).read_text(encoding="utf-8"))
        if arxiv_id in cache.get("papers", {}):
            return cache["papers"][arxiv_id]
    except (OSError, json.JSONDecodeError):
        pass
    entry = load_entry(output_dir, topic_name, arxiv_id)
    return entry["paper"] if entry else None


def saved_description(output_dir: Path, topic_name: str) -> str:
    try:
        return json.loads(_search_cache(output_dir, topic_name).read_text(
            encoding="utf-8")).get("description", "")
    except (OSError, json.JSONDecodeError):
        return ""


def fulltext_path(output_dir: Path, topic_name: str, arxiv_id: str) -> Path:
    return output_dir / topic_name / render.paper_dirname(arxiv_id) / "fulltext.txt"


def save_fulltext(paper: dict, topic_name: str, output_dir: Path) -> Path | None:
    """참고문헌을 뺀 본문 **전체**를 fulltext.txt 로 쓴다 (자르지 않는다). 실패하면 None."""
    text = fulltext.fetch_fulltext(paper, max_chars=None)
    if text is None:
        return None
    path = fulltext_path(output_dir, topic_name, paper["arxiv_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, text.text)
    return path


def describe(topic: Topic) -> str:
    kws = ", ".join(topic.keywords) or "전체"
    text = f"카테고리 {', '.join(topic.categories)} · 주제 {kws}"
    if topic.exclude_keywords:
        text += f" · 제외 {', '.join(topic.exclude_keywords)}"
    if topic.primary_category_only:
        text += " · primary 카테고리만"
    return text


def rebuild_index(output_dir: Path, topic_name: str, description: str = "") -> Path:
    topic_dir = output_dir / topic_name
    topic_dir.mkdir(parents=True, exist_ok=True)
    return render.build_index(topic_dir, topic_name, description)
