"""CLI: 검색 → (본문 추출) → claude 구독으로 한국어 리뷰 → index.html.

    python -m arxiv_review run --topic llm-agents
    python -m arxiv_review run --category cs.CL --keyword "retrieval augmented generation"
    python -m arxiv_review run --topic llm-agents --dry-run   # 검색 결과만
    python -m arxiv_review build --topic llm-agents           # HTML 만 다시
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from . import arxiv, fulltext, render, reviewer
from .config import Config, ConfigError, Topic, load_config


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def resolve_topic(args, config: Config) -> Topic:
    """--topic(config) 을 바탕으로, 명령행 값이 있으면 그것으로 덮어쓴다."""
    if args.topic and args.topic in config.topics:
        topic = config.topics[args.topic]
    elif args.category:
        topic = Topic(name=args.topic or "adhoc", categories=[])
    elif args.topic:
        raise ConfigError(
            f"config 에 토픽 {args.topic!r} 이 없습니다 "
            f"(있는 토픽: {', '.join(config.topics) or '없음'})")
    else:
        raise ConfigError("--topic 또는 --category 가 필요합니다")
    if args.category:
        topic.categories = args.category
    if args.keyword:
        topic.keywords = args.keyword
    if args.exclude:
        topic.exclude_keywords = args.exclude
    if args.primary_only:
        topic.primary_category_only = True
    if args.days:
        topic.days = args.days
    if args.max:
        topic.max_papers = args.max
    return topic.validate()


def describe(topic: Topic) -> str:
    kws = ", ".join(topic.keywords) or "전체"
    return f"카테고리 {', '.join(topic.categories)} · 주제 {kws}"


def cmd_run(args, config: Config) -> int:
    topic = resolve_topic(args, config)
    model = args.model or config.review.model
    use_fulltext = config.review.fulltext and not args.no_fulltext
    topic_dir = config.output_dir / topic.name

    print(f"[검색] {describe(topic)} · 최근 {topic.days}일 · 최대 {topic.max_papers}편")
    papers = arxiv.search(topic)
    print(f"  {len(papers)}편 발견")
    for p in papers:
        print(f"  - {p['published']} [{p['primary_category']}] {p['arxiv_id']} {p['title'][:80]}")
    if args.dry_run:
        return 0

    if use_fulltext and not fulltext.pymupdf_available():
        print("  (PyMuPDF 미설치 — 초록만으로 리뷰합니다. `pip install pymupdf` 로 본문 리뷰)")
        use_fulltext = False

    done = failed = skipped = 0
    for i, paper in enumerate(papers, 1):
        paper_dir = topic_dir / render.paper_dirname(paper["arxiv_id"])
        record = paper_dir / "paper.json"
        if record.exists() and not args.force:
            skipped += 1
            continue
        print(f"[리뷰 {i}/{len(papers)}] {paper['arxiv_id']} {paper['title'][:70]}")
        text = None
        if use_fulltext:
            text = fulltext.fetch_fulltext(paper, config.review.max_chars)
            time.sleep(arxiv.REQUEST_INTERVAL)
        try:
            review = reviewer.review_paper(
                paper, text, model=model, timeout=config.review.timeout_sec)
        except reviewer.UsageLimitError as e:
            print(f"  구독 사용 한도에 도달했습니다: {e}\n"
                  "  한도가 풀린 뒤 같은 명령을 다시 실행하면 남은 논문부터 이어집니다.",
                  file=sys.stderr)
            failed += sum(
                not (topic_dir / render.paper_dirname(q["arxiv_id"]) / "paper.json").exists()
                for q in papers[i - 1:])
            break
        except reviewer.ReviewError as e:
            print(f"  실패: {e}", file=sys.stderr)
            failed += 1
            continue
        meta = {
            "basis": "fulltext" if text else "abstract",
            "model": model,
            "topic": topic.name,
            "reviewed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        paper_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(paper_dir / "review.md", render.review_markdown(paper, review, meta))
        # paper.json 을 마지막에 쓴다 — 이 파일의 존재가 '리뷰 완료' 표시다.
        _atomic_write(record, json.dumps(
            {"paper": paper, "review": review, "meta": meta},
            ensure_ascii=False, indent=2))
        done += 1

    topic_dir.mkdir(parents=True, exist_ok=True)
    index = render.build_index(topic_dir, topic.name, describe(topic))
    print(f"[완료] 신규 리뷰 {done}편 · 기존 {skipped}편 건너뜀 · 실패/보류 {failed}편")
    print(f"  결과: {index}")
    return 1 if failed else 0


def cmd_build(args, config: Config) -> int:
    name = args.topic
    topic_dir = config.output_dir / name
    if not topic_dir.is_dir():
        raise ConfigError(f"리뷰 결과가 없습니다: {topic_dir}")
    description = describe(config.topics[name]) if name in config.topics else ""
    print(f"결과: {render.build_index(topic_dir, name, description)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="arxiv_review",
        description="arXiv 특정 카테고리·주제 검색 + Claude 구독 기반 한국어 리뷰")
    parser.add_argument("--config", default="config.json", help="설정 파일 (기본 config.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="검색 + 리뷰 + index.html")
    run.add_argument("--topic", help="config.json 의 토픽 이름 (결과 폴더 이름)")
    run.add_argument("--category", action="append", help="arXiv 카테고리 (반복 가능, 예: cs.CL)")
    run.add_argument("--keyword", action="append", help="주제 키워드 — 제목·초록 구문 검색 (반복 가능)")
    run.add_argument("--exclude", action="append", help="제외 키워드 (반복 가능)")
    run.add_argument("--primary-only", action="store_true", help="primary 카테고리가 일치하는 논문만")
    run.add_argument("--days", type=int, help="최근 N일 제출분")
    run.add_argument("--max", type=int, help="최대 논문 수")
    run.add_argument("--model", help="claude 모델 별칭/ID (기본 sonnet)")
    run.add_argument("--no-fulltext", action="store_true", help="PDF 본문 없이 초록만으로 리뷰")
    run.add_argument("--force", action="store_true", help="이미 리뷰한 논문도 다시 리뷰")
    run.add_argument("--dry-run", action="store_true", help="검색 결과만 출력 (리뷰·파일 변경 없음)")
    run.set_defaults(func=cmd_run)

    build = sub.add_parser("build", help="저장된 리뷰로 index.html 만 다시 생성")
    build.add_argument("--topic", required=True)
    build.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    try:
        return args.func(args, load_config(args.config))
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    except OSError as e:  # arXiv 네트워크 오류 등
        print(f"오류: {e}", file=sys.stderr)
        return 1
