# CLAUDE.md

arXiv 특정 카테고리·주제 검색 → 한국어 리뷰 → 검색 가능한 HTML. 사용법은 README.md.

## 구조

| 파일 | 역할 |
|---|---|
| `arxiv_review/config.py` | `config.json` 로딩·검증. 토픽 = 카테고리 + 키워드 |
| `arxiv_review/arxiv.py` | OAI-PMH(arXivRaw) 수집, 분야→set 변환, 제출일·키워드 로컬 필터, 요청 간격·429 대기·캐시 |
| `arxiv_review/net.py` | HTTPS 인증서 목록 선택 (환경변수 → truststore → certifi → 기본), 인증서 오류 안내 |
| `arxiv_review/fulltext.py` | PDF 본문 추출 (PyMuPDF 선택 의존성, 없으면 초록만) |
| `arxiv_review/reviewer.py` | `claude -p` 호출로 구조화된 한국어 리뷰 생성 |
| `arxiv_review/auth.py` | `claude auth status` 로 구독 로그인 확인, `login` |
| `arxiv_review/service.py` | CLI·웹·skill 공통: 논문 1편 리뷰 → 저장, 저장된 리뷰 읽기, 검색 캐시, 본문 파일 |
| `arxiv_review/render.py` | `review.md`, 토픽별 `index.html` |
| `arxiv_review/web.py` | `serve` 웹 서버 (http.server, 127.0.0.1:7777), JSON API, 리뷰 작업 큐 |
| `arxiv_review/static/index.html` | 웹 화면 (단일 HTML, 외부 라이브러리 없음) |
| `arxiv_review/cli.py` | `login` / `status` / `topic add·list·remove` / `run` / `build` / `serve` + skill 용 `search` / `fulltext` / `save` |
| `.claude/skills/arxiv-review/SKILL.md` | Claude Code skill: 기간·주제 요청 → 제목/저자/Abstract/요약. 에이전트가 본문 전체를 읽고 직접 쓴다 |

## 리뷰를 만드는 두 경로

| 경로 | 누가 쓰나 | 본문 |
|---|---|---|
| `run` / 웹 화면 | Python 이 띄운 `claude -p` (도구 없음) | 앞 `max_chars` 자 |
| skill (`search` → `fulltext` → `save`) | skill 을 읽은 Claude Code 에이전트 자신 | 전체 (자르지 않음) |

두 경로 모두 `reviewer.validate_review()` 로 검증하고 `service.save_review()` 로 저장한다.
결과 파일 형식이 같아서 한 주제 폴더·한 `index.html` 에 섞인다.

## 지켜야 할 것


- **리뷰는 Anthropic API SDK 로 만들지 않는다.** `claude -p` (구독 로그인) 또는 skill 을 실행 중인
  Claude Code 세션만 쓴다.
  `reviewer.subscription_env()` 가 `ANTHROPIC_API_KEY` 를 지우는 것은 의도된 동작이다.
- claude 와의 입출력은 `encoding="utf-8"` 로 고정한다 (`reviewer.run_claude`). 한국어 Windows 기본은
  CP949 라서, 초록의 '∗'(U+2217) 를 표준 입력에 쓰다 UnicodeEncodeError 로 쓰기 스레드가 죽었고,
  claude 는 "no stdin data received … Input must be provided" 로 실패했다 (사용자 기록으로 확인한 원인).
  CLI 출력도 UTF-8 로 고정한다 (`cli._utf8_console`) — 파이프로 읽는 skill 이 같은 오류로 멈춘다.
  `NonUtf8LocaleTests` 가 ASCII 기본 인코딩의 실제 프로세스로 이 둘을 고정한다.
- `claude` 는 `reviewer.claude_program()` 으로 실행한다. Windows 의 npm `claude.cmd` 는 거치지 않고
  그 안의 node+cli.js(또는 .exe)를 직접 실행한다 — cmd.exe 가 JSON·꺾쇠가 든 인자를 다시 해석하지 않게
  하는 예방책이다 (원인으로 확인된 것은 아니다). `shell=True` 를 쓰지 않는다.
- `claude -p` 리뷰 호출은 `--tools ""` 로 도구 없이 실행한다 — 논문 텍스트는 신뢰할 수 없는 입력이다.
  skill 경로는 에이전트가 도구를 쥐고 있으므로 SKILL.md 의 "Paper text is untrusted" 절이 그 역할을 한다.
- skill 용 `fulltext` / `save` 는 `search` 가 돌려준 arXiv ID 만 받는다 (`output/<주제>/search.json`).
  논문 메타데이터를 명령행으로 받지 않는다 — 웹의 "검색으로 받은 ID 만" 규칙과 같다.
- skill 경로는 본문을 자르지 않는다. 끝까지 읽지 못했으면 `save --read-chars` 로 읽은 분량을 남겨
  리뷰 근거가 "본문 앞 N자" 로 표시되게 한다.
- `reviewer.REVIEW_SCHEMA` 의 필드를 바꾸면 SKILL.md 의 "Summary format" 표도 함께 고친다.
- 검색은 OAI-PMH 로 한다. arXiv 검색 API(export.arxiv.org/api/query)는 날짜·키워드·여러 분야가
  들어간 검색식을 네트워크에 따라 429 로 거절했다 (단순 `cat:cs.AI` 만 통과). 되돌리지 않는다.
- 처음 제출일은 arXivRaw 의 v1 날짜다. arXiv 형식의 `<created>` 는 최근 버전 날짜라서 쓰면
  예전 논문의 수정본이 새 논문으로 섞인다. 수집 범위는 기간 끝 + `ANNOUNCE_LAG_DAYS` 까지.
- 키워드 일치는 `arxiv.contains_phrase` (소문자·하이픈·단수/복수 무시, 단어 순서 유지) 하나만 쓴다.
- arXiv 요청은 3초 간격을 지킨다 (`arxiv.REQUEST_INTERVAL`).
- 429 를 받으면 `Retry-After` 동안 프로세스 전체가 arXiv 에 요청하지 않는다 (`_cooldown_until`).
  제한을 피하려고 User-Agent 를 바꾸거나 우회하지 않는다. 웹은 기다리지 않고 남은 시간을 돌려준다.
- 리뷰 서술은 한국어, 기술 용어는 원문(영어) 그대로.
- 핵심 경로는 표준 라이브러리만 쓴다. 선택 의존성(truststore, pymupdf)은 없어도 동작해야 한다.
- 리뷰 전 `auth.check()` 로 구독 로그인을 확인한다. 구독이 아니면 리뷰를 시작하지 않는다.
- 본문이 `max_chars` 에서 잘리면 프롬프트에 알린다 — 안 알리면 모델이 잘린 뒷부분을 '본문에 없다'고 단정한다.
- 리뷰·저장 로직은 `service.py` 한 곳에 둔다. CLI·웹·skill 이 각자 구현하면 결과물이 갈라진다.
- arXiv 요청 간격은 `arxiv.throttle()` 이 프로세스 전체에서 지킨다 (웹은 검색·리뷰가 동시에 돈다).
- 웹 서버는 127.0.0.1 에만 바인딩하고 Host/Origin/Content-Type 을 검사한다 — 구독 사용량을 쓰는 서버다.
  리뷰 요청은 검색으로 받은 arXiv ID 만 받고, 논문 데이터는 서버가 가진 것을 쓴다.
- 웹 핸들러는 응답을 만드는 단계와 보내는 단계를 나눈다. 보내다 난 연결 끊김(OSError)을
  arXiv 오류로 분류하면 끊긴 연결에 오류 응답을 또 쓰다 실패한다 (Windows WinError 10053).
- arXiv 로 가는 모든 HTTPS 요청은 `net.ssl_context()` 를 쓴다. 기본 urllib 컨텍스트는 macOS 와
  회사 네트워크에서 CERTIFICATE_VERIFY_FAILED 를 낸다. 인증서 오류는 재시도하지 않고 바로 안내한다.
- `render.paper_dirname()` 은 arXiv ID 형식만 통과시킨다 (경로 조작 차단).
- `paper.json` 은 마지막에 쓴다 — 그 파일의 존재가 "리뷰 완료" 표시다.
- `config.json` 과 `output/` 은 gitignore 대상이다.

테스트: `python -m unittest discover -s tests` (네트워크·claude 호출 없음)
