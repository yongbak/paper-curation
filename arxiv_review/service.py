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
    meta = {
        "basis": "fulltext" if text else "abstract",
        "model": opts.model,
        "topic": topic_name,
        "reviewed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if text:
        meta["fulltext_chars"] = len(text.text)
        meta["fulltext_total_chars"] = text.total_chars
    entry = {"paper": paper, "review": review, "meta": meta}
    record = entry_path(output_dir, topic_name, paper["arxiv_id"])
    record.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(record.parent / "review.md", render.review_markdown(paper, review, meta))
    # paper.json 을 마지막에 쓴다 — 이 파일의 존재가 '리뷰 완료' 표시다.
    _atomic_write(record, json.dumps(entry, ensure_ascii=False, indent=2))
    return entry


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
