"""로컬 웹 UI 서버 (표준 라이브러리 http.server).

    python -m arxiv_review serve            # http://localhost:7777

- 127.0.0.1 에만 바인딩한다. 이 서버는 사용자의 Claude 구독으로 리뷰를 돌리므로
  다른 기기나 다른 웹사이트가 호출할 수 있으면 안 된다.
- Host / Origin 헤더를 검사하고 POST 는 application/json 만 받는다
  (DNS rebinding 과 다른 사이트의 폼 전송 차단).
- 리뷰는 백그라운드 작업 큐 하나에서 한 편씩 순차 처리한다 (CLI 와 같은 service 함수).
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import queue
import re
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import arxiv, auth, fulltext, render, reviewer, service
from .config import (TOPIC_RE, ConfigError, Topic, delete_topic, load_config, save_topic,
                     topic_to_dict)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 1_000_000
LOGIN_CACHE_SEC = 60
CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)
MAX_WEB_PAPERS = 200
DEFAULT_PORT = 7777


class BadRequest(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST, **extra):
        super().__init__(message)
        self.status, self.extra = status, extra


def _str_list(value, key: str) -> list[str]:
    """목록이나 문자열 모두 받는다. 문자열이면 카테고리는 쉼표·공백, 키워드는 쉼표·줄바꿈으로 나눈다."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\s]+" if key == "categories" else r"[,\n]+", value)
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        parts = value
    else:
        raise BadRequest(f"{key} 는 문자열 목록이어야 합니다")
    return [p.strip() for p in parts if p.strip()]


def _int(value, key: str, default: int, lo: int, hi: int) -> int:
    if value in (None, ""):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{key} 는 정수여야 합니다") from None
    if not lo <= n <= hi:
        raise BadRequest(f"{key} 는 {lo}~{hi} 사이여야 합니다")
    return n


def _date(value, key: str) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise BadRequest(f"{key} 는 YYYY-MM-DD 형식이어야 합니다") from None


def topic_from_request(body: dict) -> Topic:
    try:
        return Topic(
            name=str(body.get("topic") or "web").strip(),
            categories=_str_list(body.get("categories"), "categories"),
            keywords=_str_list(body.get("keywords"), "keywords"),
            exclude_keywords=_str_list(body.get("exclude"), "exclude"),
            primary_category_only=bool(body.get("primary_only")),
            days=_int(body.get("days"), "days", 7, 1, 3650),
            max_papers=_int(body.get("max"), "max", 10, 1, MAX_WEB_PAPERS),
        ).validate()
    except ConfigError as e:
        raise BadRequest(str(e)) from None


def entry_for_client(entry: dict | None) -> dict | None:
    """리뷰 마크다운을 서버에서 HTML 로 바꿔 보낸다 (이스케이프는 render.md_to_html 이 한다)."""
    if not entry:
        return None
    review, meta = entry["review"], entry.get("meta", {})
    return {
        "review_html": {k: render.md_to_html(v) for k, v in review.items() if isinstance(v, str)},
        "scores": {k: review[k] for k, _ in render.SCORES if k in review},
        "basis": render.basis_label(meta),
        "model": meta.get("model", ""),
        "reviewed_at": meta.get("reviewed_at", ""),
    }


class App:
    """서버 상태: 검색으로 본 논문, 리뷰 작업 큐."""

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)
        self.lock = threading.Lock()
        self.papers: dict[str, dict] = {}   # 검색에서 받은 논문 (리뷰 요청은 ID 로만 받는다)
        self.jobs: dict[str, dict] = {}
        self._login_cache = None
        self.descriptions: dict[str, str] = {}  # 주제 이름 → 마지막 검색 조건 (index.html 부제)
        self.queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def config(self):
        # 매 요청 다시 읽는다 — 터미널에서 topic add 한 내용도 바로 보인다.
        return load_config(self.config_path)

    # ---- API -----------------------------------------------------------
    def login_status(self) -> auth.LoginStatus:
        """페이지를 열 때마다 claude 를 두 번 실행하면 느리다 (Windows 에서 수 초).
        정상 결과만 잠시 재사용한다 — 문제 상태는 매번 다시 봐야 로그인 직후 새로고침에 반영된다."""
        with self.lock:
            cached = self._login_cache
        if cached and time.monotonic() - cached[0] < LOGIN_CACHE_SEC:
            return cached[1]
        login = auth.check()
        with self.lock:
            self._login_cache = (time.monotonic(), login) if login.problem() is None else None
        return login

    def status(self) -> dict:
        login = self.login_status()
        cfg = self.config()
        return {
            "login": {"installed": login.installed, "logged_in": login.logged_in,
                      "method": login.method, "subscription": login.subscription,
                      "problem": login.problem()},
            "pymupdf": fulltext.pymupdf_available(),
            "defaults": {"model": cfg.review.model, "fulltext": cfg.review.fulltext},
            "output_dir": str(cfg.output_dir),
            "today": dt.datetime.now(dt.timezone.utc).date().isoformat(),
        }

    def topics(self) -> dict:
        return {"topics": {n: topic_to_dict(t) for n, t in self.config().topics.items()}}

    def save_topic(self, body: dict) -> dict:
        if not str(body.get("topic") or "").strip():
            raise BadRequest("저장할 주제 이름을 입력하세요")
        topic = topic_from_request(body)
        try:
            save_topic(self.config_path, topic)
        except ConfigError as e:
            raise BadRequest(str(e)) from None
        return {"saved": topic.name, **self.topics()}

    def delete_topic(self, body: dict) -> dict:
        name = str(body.get("topic") or "")
        if not delete_topic(self.config_path, name):
            raise BadRequest(f"등록된 주제 중에 {name!r} 이 없습니다", HTTPStatus.NOT_FOUND)
        return self.topics()

    def search(self, body: dict) -> dict:
        topic = topic_from_request(body)
        since, until = _date(body.get("date_from"), "시작일"), _date(body.get("date_to"), "종료일")
        try:
            since, until = arxiv.date_window(topic, since=since, until=until)
        except ValueError as e:
            raise BadRequest(str(e)) from None
        # 웹은 429 에 기다리지 않는다 — 화면이 몇 분씩 멈추는 대신 남은 시간을 바로 알려 준다
        result = arxiv.search_detailed(
            topic, since=since, until=until,
            fetch=lambda url: arxiv._fetch(url, retries=1, wait_on_limit=False))
        cfg = self.config()
        with self.lock:
            for p in result.papers:
                self.papers[p["arxiv_id"]] = p
            self.descriptions[topic.name] = service.describe(topic)
        return {
            "topic": topic.name,
            "description": service.describe(topic),
            "since": result.since.isoformat(), "until": result.until.isoformat(),
            "more_available": result.more_available, "oldest": result.oldest,
            "max": topic.max_papers,
            "results": [{"paper": p, "review": entry_for_client(
                service.load_entry(cfg.output_dir, topic.name, p["arxiv_id"]))}
                for p in result.papers],
        }

    def start_review(self, body: dict) -> dict:
        name = str(body.get("topic") or "web").strip()
        if not TOPIC_RE.match(name):
            raise BadRequest(f"주제 이름은 영문·숫자·-·_ 만 가능합니다: {name!r}")
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise BadRequest("리뷰할 논문 ID 목록(ids)이 필요합니다")
        with self.lock:
            missing = [i for i in ids if i not in self.papers]
            papers = [self.papers[i] for i in ids if i in self.papers]
        if missing:
            raise BadRequest(f"먼저 검색으로 불러온 논문만 리뷰할 수 있습니다: {', '.join(missing[:3])}")
        if not body.get("skip_login_check"):
            problem = auth.check().problem()
            if problem:
                raise BadRequest(problem, HTTPStatus.CONFLICT, login_problem=True)
        cfg = self.config()
        use_fulltext = bool(body.get("fulltext", cfg.review.fulltext)) and fulltext.pymupdf_available()
        opts = service.ReviewOptions(
            model=str(body.get("model") or cfg.review.model), use_fulltext=use_fulltext,
            max_chars=cfg.review.max_chars, timeout=cfg.review.timeout_sec)
        job = {
            "id": uuid.uuid4().hex[:12], "topic": name, "state": "queued", "message": "",
            "items": [{"id": p["arxiv_id"], "title": p["title"], "status": "pending",
                       "message": "", "review": None} for p in papers],
        }
        with self.lock:
            self.jobs[job["id"]] = job
        self.queue.put((job["id"], papers, opts, bool(body.get("force")),
                        cfg.output_dir, self.descriptions.get(name, "")))
        return self.job(job["id"])

    def job(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise BadRequest("작업을 찾을 수 없습니다", HTTPStatus.NOT_FOUND)
            return copy.deepcopy(self.jobs[job_id])

    # ---- 백그라운드 작업 ----------------------------------------------
    def _set(self, job_id: str, index: int | None = None, **fields) -> None:
        with self.lock:
            target = self.jobs[job_id] if index is None else self.jobs[job_id]["items"][index]
            target.update(fields)

    def _worker(self) -> None:
        while True:
            job_id, papers, opts, force, output_dir, description = self.queue.get()
            try:
                self._run_job(job_id, papers, opts, force, output_dir, description)
            except Exception as e:  # 작업 하나가 죽어도 서버는 계속
                self._set(job_id, state="error", message=str(e))

    def _run_job(self, job_id, papers, opts, force, output_dir, description) -> None:
        topic_name = self.jobs[job_id]["topic"]
        self._set(job_id, state="running")
        final = {"state": "done"}
        for i, paper in enumerate(papers):
            existing = None if force else service.load_entry(output_dir, topic_name, paper["arxiv_id"])
            if existing:
                self._set(job_id, i, status="done", message="이미 리뷰한 논문",
                          review=entry_for_client(existing))
                continue
            self._set(job_id, i, status="running",
                      message="본문을 읽고 리뷰하는 중" if opts.use_fulltext else "초록으로 리뷰하는 중")
            try:
                entry = service.review_and_save(
                    paper, topic_name, output_dir, opts,
                    log=lambda msg, i=i: self._set(job_id, i, message=msg.strip()))
            except reviewer.UsageLimitError as e:
                with self.lock:
                    for item in self.jobs[job_id]["items"][i:]:
                        item.update(status="held", message="구독 사용 한도 — 나중에 다시 리뷰하세요")
                final = {"state": "stopped", "message": f"구독 사용 한도에 도달했습니다: {e}"}
                break
            except reviewer.ReviewError as e:
                self._set(job_id, i, status="error", message=str(e))
                continue
            self._set(job_id, i, status="done", message="", review=entry_for_client(entry))
        # 결과 HTML 을 먼저 만들고 나서 끝났다고 알린다 (반대로 하면 화면이 '완료' 인데 파일이 없다)
        service.rebuild_index(output_dir, topic_name, description)
        self._set(job_id, **final)


def make_handler(app: App, port: int):
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "arxiv-review"

        def log_message(self, fmt, *args):  # 폴링 로그로 터미널을 채우지 않는다
            if not self.path.startswith("/api/jobs/"):
                sys.stderr.write(f"[web] {self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            try:
                self.end_headers()
                self.wfile.write(body)
            except CLIENT_GONE:
                # 응답 전에 브라우저가 연결을 끊었다 (새로고침·탭 닫기). 받을 쪽이 없으니 그냥 끝낸다.
                self.close_connection = True

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _guard(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._json(HTTPStatus.FORBIDDEN, {"error": "허용되지 않은 Host"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in allowed_origins:
                self._json(HTTPStatus.FORBIDDEN, {"error": "허용되지 않은 Origin"})
                return False
            return True

        def _handle(self, fn) -> None:
            # 응답을 만드는 단계의 오류만 분류한다. 보내는 단계의 연결 끊김(OSError)을
            # arXiv 네트워크 오류로 착각하면 끊긴 연결에 오류 응답을 또 쓰려다 실패한다.
            try:
                status, data = HTTPStatus.OK, fn()
            except BadRequest as e:
                status, data = e.status, {"error": str(e), **e.extra}
            except ConfigError as e:
                status, data = HTTPStatus.BAD_REQUEST, {"error": str(e)}
            except arxiv.ArxivUnavailable as e:  # 429 제한, 인증서 문제 — 안내문을 그대로
                status, data = HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": str(e), "retry_after": getattr(e, "retry_after", None)}
            except OSError as e:  # arXiv 네트워크 오류 등
                status, data = HTTPStatus.BAD_GATEWAY, {"error": f"네트워크 오류: {e}"}
            self._json(status, data)

        def do_GET(self):
            if not self._guard():
                return
            path = urlparse(self.path).path
            if path == "/":
                self._send(HTTPStatus.OK, (STATIC_DIR / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif path == "/api/status":
                self._handle(app.status)
            elif path == "/api/topics":
                self._handle(app.topics)
            elif path.startswith("/api/jobs/"):
                self._handle(lambda: app.job(path.rsplit("/", 1)[-1]))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "없는 경로"})

        def do_POST(self):
            if not self._guard():
                return
            if self.headers.get_content_type() != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "application/json 만 받습니다"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "요청이 너무 큽니다"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON 객체가 아닙니다"})
                return
            routes = {"/api/search": app.search, "/api/review": app.start_review,
                      "/api/topics": app.save_topic, "/api/topics/delete": app.delete_topic}
            fn = routes.get(urlparse(self.path).path)
            if fn is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "없는 경로"})
            else:
                self._handle(lambda: fn(body))

    return Handler


def serve(config_path: str | Path, port: int = DEFAULT_PORT) -> int:
    app = App(config_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    server.daemon_threads = True
    print(f"arXiv 리뷰 웹 UI: http://localhost:{port}  (종료: Ctrl+C)")
    print(f"  설정 파일: {Path(config_path).resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
    finally:
        server.server_close()
    return 0
