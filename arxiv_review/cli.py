"""CLI.

    python -m arxiv_review login                       # Pro 구독 계정 로그인 (최초 1회)
    python -m arxiv_review status                      # 로그인 방식 확인
    python -m arxiv_review topic add 내토픽 --category cs.CL --keyword "LLM agent"
    python -m arxiv_review topic list
    python -m arxiv_review run --topic 내토픽 --dry-run  # 검색 결과만 확인
    python -m arxiv_review run --topic 내토픽            # 검색 + 리뷰 + index.html
    python -m arxiv_review build --topic 내토픽          # HTML 만 다시
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from . import arxiv, auth, fulltext, render, reviewer
from .config import (Config, ConfigError, Topic, delete_topic, load_config,
                     save_topic)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _apply_overrides(topic: Topic, args) -> Topic:
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


def resolve_topic(args, config: Config) -> Topic:
    """--topic(config) 을 바탕으로, 명령행 값이 있으면 그것으로 덮어쓴다."""
    if args.topic and args.topic in config.topics:
        topic = config.topics[args.topic]
    elif args.category:
        topic = Topic(name=args.topic or "adhoc", categories=[])
    elif args.topic:
        raise ConfigError(
            f"등록된 검색 주제 중에 {args.topic!r} 이 없습니다 "
            f"(등록된 주제: {', '.join(config.topics) or '없음'}).\n"
            f"  먼저 등록하세요: python -m arxiv_review topic add {args.topic} "
            "--category cs.CL --keyword \"찾을 주제\"")
    else:
        raise ConfigError(
            "검색 주제가 없습니다. --topic <등록한 이름> 또는 --category/--keyword 를 주세요.\n"
            "  등록 방법: python -m arxiv_review topic add --help")
    return _apply_overrides(topic, args)


def describe(topic: Topic) -> str:
    kws = ", ".join(topic.keywords) or "전체"
    text = f"카테고리 {', '.join(topic.categories)} · 주제 {kws}"
    if topic.exclude_keywords:
        text += f" · 제외 {', '.join(topic.exclude_keywords)}"
    if topic.primary_category_only:
        text += " · primary 카테고리만"
    return text


def cmd_run(args, config: Config) -> int:
    topic = resolve_topic(args, config)
    model = args.model or config.review.model
    use_fulltext = config.review.fulltext and not args.no_fulltext
    topic_dir = config.output_dir / topic.name

    if not args.dry_run and not args.skip_login_check:
        problem = auth.check().problem()
        if problem:
            print(f"[로그인 확인] {problem}", file=sys.stderr)
            return 2

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
            if text and text.truncated:
                print(f"  본문 {text.total_chars:,}자 중 앞 {len(text.text):,}자만 사용 "
                      "(config.json 의 review.max_chars 로 조정)")
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
        if text:
            meta["fulltext_chars"] = len(text.text)
            meta["fulltext_total_chars"] = text.total_chars
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


def cmd_topic_add(args, config: Config) -> int:
    if not args.category:
        raise ConfigError("--category 를 하나 이상 주세요 (예: --category cs.CL)")
    base = config.topics.get(args.name) or Topic(name=args.name, categories=[])
    topic = _apply_overrides(base, args)
    save_topic(args.config, topic)
    verb = "수정" if args.name in config.topics else "등록"
    print(f"검색 주제 {topic.name!r} {verb} → {Path(args.config).resolve()}")
    print(f"  {describe(topic)} · 최근 {topic.days}일 · 최대 {topic.max_papers}편")
    print(f"  검색 결과 확인: python -m arxiv_review run --topic {topic.name} --dry-run")
    return 0


def cmd_topic_list(args, config: Config) -> int:
    if not config.topics:
        print("등록된 검색 주제가 없습니다. 등록: python -m arxiv_review topic add --help")
        return 0
    for t in config.topics.values():
        print(f"{t.name}\n  {describe(t)} · 최근 {t.days}일 · 최대 {t.max_papers}편")
    return 0


def cmd_topic_remove(args, config: Config) -> int:
    if not delete_topic(args.config, args.name):
        raise ConfigError(f"등록된 검색 주제 중에 {args.name!r} 이 없습니다")
    print(f"검색 주제 {args.name!r} 삭제 (이미 만든 리뷰 결과는 그대로 둡니다)")
    return 0


def cmd_login(args, config: Config) -> int:
    print("브라우저가 열리면 Claude Pro 구독 계정으로 로그인하세요.")
    code = auth.login()
    return cmd_status(args, config) if code == 0 else code


def cmd_status(args, config: Config) -> int:
    status = auth.check()
    problem = status.problem()
    if status.logged_in:
        print(f"Login method: {status.method or '알 수 없음'}")
    if problem:
        print(problem, file=sys.stderr)
        return 1
    print("구독 계정으로 로그인돼 있습니다. 리뷰는 구독 사용량으로 처리됩니다.")
    return 0


def _add_filter_args(p, *, topic_cmd: bool) -> None:
    p.add_argument("--category", action="append",
                   help="arXiv 카테고리 (반복 가능, 예: --category cs.CL --category cs.AI)")
    p.add_argument("--keyword", action="append",
                   help="찾을 주제. 제목·초록 구문 검색 (반복 가능, 여러 개면 OR)")
    p.add_argument("--exclude", action="append", help="제외 키워드 (반복 가능)")
    p.add_argument("--primary-only", action="store_true",
                   help="primary 카테고리가 일치하는 논문만 (cross-list 제외)")
    p.add_argument("--days", type=int, help="최근 N일 제출분" + (" (기본 7)" if topic_cmd else ""))
    p.add_argument("--max", type=int, help="최대 논문 수" + (" (기본 10)" if topic_cmd else ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="arxiv_review",
        description="arXiv 특정 카테고리·주제 검색 + Claude 구독 기반 한국어 리뷰")
    parser.add_argument("--config", default="config.json", help="설정 파일 (기본 config.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="Claude Pro 구독 계정으로 로그인").set_defaults(func=cmd_login)
    sub.add_parser("status", help="로그인 방식(구독 여부) 확인").set_defaults(func=cmd_status)

    topic = sub.add_parser("topic", help="검색 주제 등록·조회·삭제 (config.json)")
    tsub = topic.add_subparsers(dest="topic_command", required=True)
    add = tsub.add_parser("add", help="검색 주제 등록 (같은 이름이면 수정)")
    add.add_argument("name", help="주제 이름 (영문·숫자·-·_). 결과 폴더 이름이 된다")
    _add_filter_args(add, topic_cmd=True)
    add.set_defaults(func=cmd_topic_add)
    tsub.add_parser("list", help="등록된 검색 주제 보기").set_defaults(func=cmd_topic_list)
    rm = tsub.add_parser("remove", help="검색 주제 삭제")
    rm.add_argument("name")
    rm.set_defaults(func=cmd_topic_remove)

    run = sub.add_parser("run", help="검색 + 리뷰 + index.html")
    run.add_argument("--topic", help="등록한 검색 주제 이름 (topic add 로 등록)")
    _add_filter_args(run, topic_cmd=False)
    run.add_argument("--model", help="claude 모델 별칭/ID (기본 sonnet)")
    run.add_argument("--no-fulltext", action="store_true", help="PDF 본문 없이 초록만으로 리뷰")
    run.add_argument("--force", action="store_true", help="이미 리뷰한 논문도 다시 리뷰")
    run.add_argument("--dry-run", action="store_true", help="검색 결과만 출력 (리뷰·파일 변경 없음)")
    run.add_argument("--skip-login-check", action="store_true",
                     help="리뷰 전 구독 로그인 확인을 건너뜀")
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
