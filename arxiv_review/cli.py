"""CLI.

    python -m arxiv_review login                       # Pro 구독 계정 로그인 (최초 1회)
    python -m arxiv_review status                      # 로그인 방식 확인
    python -m arxiv_review topic add 내토픽 --category cs.CL --keyword "LLM agent"
    python -m arxiv_review topic list
    python -m arxiv_review run --topic 내토픽 --dry-run  # 검색 결과만 확인
    python -m arxiv_review run --topic 내토픽            # 검색 + 리뷰 + index.html
    python -m arxiv_review build --topic 내토픽          # HTML 만 다시
    python -m arxiv_review serve                        # 웹 UI: http://localhost:7777

Claude Code skill(.claude/skills/arxiv-review) 이 쓰는 단계별 명령 — 리뷰는 에이전트가 쓴다:
    python -m arxiv_review search --topic 이름 --category cs.CL --keyword "RAG" \
        --from 2026-11-01 --to 2026-11-09               # 검색 결과 JSON
    python -m arxiv_review fulltext 2611.01234 --topic 이름   # 본문 전체 → fulltext.txt
    python -m arxiv_review save 2611.01234 --topic 이름 --review draft.json --basis fulltext
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import arxiv, auth, fulltext, render, reviewer, service
from .service import describe
from .config import (Config, ConfigError, Topic, delete_topic, load_config,
                     save_topic)


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


def parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as e:
        raise ConfigError(f"날짜는 YYYY-MM-DD 형식이어야 합니다: {value!r}") from e


def cmd_run(args, config: Config) -> int:
    topic = resolve_topic(args, config)
    model = args.model or config.review.model
    use_fulltext = config.review.fulltext and not args.no_fulltext

    if not args.dry_run and not args.skip_login_check:
        problem = auth.check().problem()
        if problem:
            print(f"[로그인 확인] {problem}", file=sys.stderr)
            return 2

    since, until = parse_date(args.date_from), parse_date(args.date_to)
    try:
        since, until = arxiv.date_window(topic, since=since, until=until)
    except ValueError as e:
        raise ConfigError(str(e)) from e
    print(f"[검색] {describe(topic)} · {since} ~ {until} · 최대 {topic.max_papers}편")
    try:
        result = arxiv.search_detailed(topic, since=since, until=until, log=print)
    except ValueError as e:  # arXiv 에 없는 분야
        raise ConfigError(str(e)) from e
    papers = result.papers
    print(f"  arXiv 기록 {result.scanned:,}건 중 조건에 맞는 {len(papers)}편")
    if result.more_available:
        print(f"  ※ 최대 {topic.max_papers}편에서 잘렸습니다. 기간 안에 논문이 더 있지만 "
              f"{result.oldest} 이후 것만 포함됩니다. 기간 전체를 보려면 --max 를 늘리세요.")
    for p in papers:
        print(f"  - {p['published']} [{p['primary_category']}] {p['arxiv_id']} {p['title'][:80]}")
    if args.dry_run:
        return 0

    if use_fulltext and not fulltext.pymupdf_available():
        print("  (PyMuPDF 미설치 — 초록만으로 리뷰합니다. `pip install pymupdf` 로 본문 리뷰)")
        use_fulltext = False
    opts = service.ReviewOptions(model=model, use_fulltext=use_fulltext,
                                 max_chars=config.review.max_chars,
                                 timeout=config.review.timeout_sec)

    done = failed = skipped = 0
    for i, paper in enumerate(papers, 1):
        if not args.force and service.load_entry(config.output_dir, topic.name, paper["arxiv_id"]):
            skipped += 1
            continue
        print(f"[리뷰 {i}/{len(papers)}] {paper['arxiv_id']} {paper['title'][:70]}")
        try:
            service.review_and_save(paper, topic.name, config.output_dir, opts)
        except reviewer.UsageLimitError as e:
            print(f"  구독 사용 한도에 도달했습니다: {e}\n"
                  "  한도가 풀린 뒤 같은 명령을 다시 실행하면 남은 논문부터 이어집니다.",
                  file=sys.stderr)
            failed += sum(
                not service.load_entry(config.output_dir, topic.name, q["arxiv_id"])
                for q in papers[i - 1:])
            break
        except reviewer.ReviewError as e:
            print(f"  실패: {e}", file=sys.stderr)
            failed += 1
            continue
        done += 1

    index = service.rebuild_index(config.output_dir, topic.name, describe(topic))
    print(f"[완료] 신규 리뷰 {done}편 · 기존 {skipped}편 건너뜀 · 실패/보류 {failed}편")
    print(f"  결과: {index}")
    return 1 if failed else 0


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _topic_name(name: str) -> str:
    return Topic(name=name, categories=["cs.AI"]).validate().name  # 이름 형식만 검사


def _searched(config: Config, topic_name: str, arxiv_id: str) -> dict:
    try:
        paper = service.searched_paper(config.output_dir, topic_name, arxiv_id)
    except ValueError as e:  # arXiv ID 형식이 아님
        raise ConfigError(str(e)) from e
    if paper is None:
        raise ConfigError(
            f"{arxiv_id} 는 주제 {topic_name!r} 의 검색 결과에 없습니다. "
            "먼저 `python -m arxiv_review search --topic ...` 로 검색하세요.")
    return paper


def cmd_search(args, config: Config) -> int:
    """검색 결과를 JSON 으로 출력하고, fulltext/save 가 쓸 수 있게 기억해 둔다."""
    topic = resolve_topic(args, config)
    since, until = parse_date(args.date_from), parse_date(args.date_to)
    try:
        result = arxiv.search_detailed(topic, since=since, until=until)
    except ValueError as e:
        raise ConfigError(str(e)) from e
    service.remember_search(config.output_dir, topic.name, result.papers, describe(topic))
    keys = ("arxiv_id", "title", "authors", "abstract", "published",
            "primary_category", "categories", "abs_url")
    _print_json({
        "topic": topic.name,
        "query": describe(topic),
        "since": str(result.since), "until": str(result.until),
        "max_papers": topic.max_papers,
        "more_available": result.more_available,
        "oldest_included": result.oldest,
        "papers": [
            {**{k: p[k] for k in keys},
             "reviewed": bool(service.load_entry(config.output_dir, topic.name, p["arxiv_id"]))}
            for p in result.papers],
    })
    return 0


def cmd_fulltext(args, config: Config) -> int:
    """논문 본문 전체(참고문헌 제외)를 파일로 쓰고 경로를 JSON 으로 알려 준다."""
    name = _topic_name(args.topic)
    paper = _searched(config, name, args.arxiv_id)
    if not fulltext.pymupdf_available():
        _print_json({"arxiv_id": args.arxiv_id, "path": None,
                     "reason": "PyMuPDF 미설치 (pip install pymupdf)"})
        return 0
    path = service.save_fulltext(paper, name, config.output_dir)
    if path is None:
        _print_json({"arxiv_id": args.arxiv_id, "path": None,
                     "reason": "PDF 를 받지 못했거나 텍스트를 추출할 수 없습니다"})
        return 0
    text = path.read_text(encoding="utf-8")
    _print_json({"arxiv_id": args.arxiv_id, "path": str(path),
                 "chars": len(text), "lines": text.count("\n") + 1})
    return 0


def cmd_save(args, config: Config) -> int:
    """에이전트가 쓴 리뷰 JSON 을 검증해 review.md + paper.json 으로 저장한다."""
    name = _topic_name(args.topic)
    paper = _searched(config, name, args.arxiv_id)
    if not args.force and service.load_entry(config.output_dir, name, args.arxiv_id):
        raise ConfigError(f"{args.arxiv_id} 는 이미 리뷰가 저장돼 있습니다. 다시 쓰려면 --force")
    try:
        draft = json.loads(Path(args.review).read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"리뷰 파일을 읽을 수 없습니다: {e}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"{args.review}: JSON 오류 — {e}") from e
    try:
        review = reviewer.validate_review(draft)
    except reviewer.ReviewError as e:
        raise ConfigError(f"리뷰 형식 오류 — {e}") from e

    read = total = None
    if args.basis == "fulltext":
        body = service.fulltext_path(config.output_dir, name, args.arxiv_id)
        if not body.exists():
            raise ConfigError(
                f"본문 파일이 없습니다: {body}\n  `fulltext {args.arxiv_id} --topic {name}` 을 "
                "먼저 실행하거나, 초록만 읽었다면 --basis abstract 로 저장하세요.")
        total = len(body.read_text(encoding="utf-8"))
        read = total if args.read_chars is None else args.read_chars
        if not 0 < read <= total:
            raise ConfigError(f"--read-chars 는 1 ~ {total} 이어야 합니다")
    elif args.read_chars is not None:
        raise ConfigError("--read-chars 는 --basis fulltext 와 함께 씁니다")

    service.save_review(paper, review, name, config.output_dir, model=args.model,
                        fulltext_chars=read, fulltext_total_chars=total)
    index = service.rebuild_index(
        config.output_dir, name,
        describe(config.topics[name]) if name in config.topics
        else service.saved_description(config.output_dir, name))
    record = service.entry_path(config.output_dir, name, args.arxiv_id)
    _print_json({"arxiv_id": args.arxiv_id, "saved": str(record.parent / "review.md"),
                 "index": str(index)})
    return 0


def cmd_build(args, config: Config) -> int:
    name = args.topic
    topic_dir = config.output_dir / name
    if not topic_dir.is_dir():
        raise ConfigError(f"리뷰 결과가 없습니다: {topic_dir}")
    description = (describe(config.topics[name]) if name in config.topics
                   else service.saved_description(config.output_dir, name))
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
    from . import net
    print(f"arXiv 연결: 인증서 목록 = {net.trust_source()}")
    try:
        arxiv._fetch(arxiv.OAI_URL + "?verb=Identify", retries=1)
        print("  arXiv 접속 정상")
    except OSError as e:
        print(f"  arXiv 접속 실패: {e}", file=sys.stderr)
    status = auth.check()
    problem = status.problem()
    if status.logged_in:
        print(f"Login method: {status.method or '알 수 없음'}")
    if problem:
        print(problem, file=sys.stderr)
        return 1
    print("구독 계정으로 로그인돼 있습니다. 리뷰는 구독 사용량으로 처리됩니다.")
    return 0


def cmd_serve(args, config: Config) -> int:
    from .web import serve
    return serve(args.config, port=args.port)


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
    run.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD",
                     help="검색 시작일 (제출일 기준). 주면 --days 대신 쓴다")
    run.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD",
                     help="검색 종료일 (기본 오늘)")
    run.add_argument("--model", help="claude 모델 별칭/ID (기본 sonnet)")
    run.add_argument("--no-fulltext", action="store_true", help="PDF 본문 없이 초록만으로 리뷰")
    run.add_argument("--force", action="store_true", help="이미 리뷰한 논문도 다시 리뷰")
    run.add_argument("--dry-run", action="store_true", help="검색 결과만 출력 (리뷰·파일 변경 없음)")
    run.add_argument("--skip-login-check", action="store_true",
                     help="리뷰 전 구독 로그인 확인을 건너뜀")
    run.set_defaults(func=cmd_run)

    # ── Claude Code skill 용 단계별 명령 (리뷰는 에이전트가 쓰고, 여기서는 검증·저장만) ──
    search = sub.add_parser("search", help="검색 결과를 JSON 으로 출력 (리뷰 없음)")
    search.add_argument("--topic", help="주제 이름. 등록돼 있지 않으면 --category 와 함께 준다")
    _add_filter_args(search, topic_cmd=False)
    search.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="검색 시작일")
    search.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="검색 종료일 (기본 오늘)")
    search.set_defaults(func=cmd_search)

    ft = sub.add_parser("fulltext", help="논문 본문 전체를 fulltext.txt 로 저장")
    ft.add_argument("arxiv_id")
    ft.add_argument("--topic", required=True)
    ft.set_defaults(func=cmd_fulltext)

    save = sub.add_parser("save", help="작성된 리뷰 JSON 을 검증해 저장하고 index.html 갱신")
    save.add_argument("arxiv_id")
    save.add_argument("--topic", required=True)
    save.add_argument("--review", required=True, metavar="FILE", help="리뷰 JSON 파일")
    save.add_argument("--basis", required=True, choices=["fulltext", "abstract"],
                      help="무엇을 읽고 썼는지")
    save.add_argument("--read-chars", type=int, metavar="N",
                      help="본문을 끝까지 읽지 못했을 때 실제로 읽은 글자 수")
    save.add_argument("--model", default="claude-code",
                      help="리뷰를 쓴 모델 표시 (기본 claude-code)")
    save.add_argument("--force", action="store_true", help="이미 저장된 리뷰를 덮어씀")
    save.set_defaults(func=cmd_save)

    build = sub.add_parser("build", help="저장된 리뷰로 index.html 만 다시 생성")
    build.add_argument("--topic", required=True)
    build.set_defaults(func=cmd_build)

    srv = sub.add_parser("serve", help="웹 UI 서버 실행 (기본 http://localhost:7777)")
    srv.add_argument("--port", type=int, default=7777, help="포트 (기본 7777)")
    srv.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    try:
        return args.func(args, load_config(args.config))
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    except OSError as e:  # arXiv 네트워크 오류 등
        print(f"오류: {e}", file=sys.stderr)
        return 1
