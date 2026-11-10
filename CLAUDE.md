# CLAUDE.md

arXiv 특정 카테고리·주제 검색 → 한국어 리뷰 → 검색 가능한 HTML. 사용법은 README.md.

## 구조

| 파일 | 역할 |
|---|---|
| `arxiv_review/config.py` | `config.json` 로딩·검증. 토픽 = 카테고리 + 키워드 |
| `arxiv_review/arxiv.py` | arXiv API 검색식 생성, Atom 파싱, 후처리 필터 |
| `arxiv_review/net.py` | HTTPS 인증서 목록 선택 (환경변수 → truststore → certifi → 기본), 인증서 오류 안내 |
| `arxiv_review/fulltext.py` | PDF 본문 추출 (PyMuPDF 선택 의존성, 없으면 초록만) |
| `arxiv_review/reviewer.py` | `claude -p` 호출로 구조화된 한국어 리뷰 생성 |
| `arxiv_review/auth.py` | `claude auth status` 로 구독 로그인 확인, `login` |
| `arxiv_review/service.py` | CLI·웹 공통: 논문 1편 리뷰 → 저장, 저장된 리뷰 읽기 |
| `arxiv_review/render.py` | `review.md`, 토픽별 `index.html` |
| `arxiv_review/web.py` | `serve` 웹 서버 (http.server, 127.0.0.1:7777), JSON API, 리뷰 작업 큐 |
| `arxiv_review/static/index.html` | 웹 화면 (단일 HTML, 외부 라이브러리 없음) |
| `arxiv_review/cli.py` | `login` / `status` / `topic add·list·remove` / `run` / `build` / `serve` |

## 지켜야 할 것

- **리뷰는 Anthropic API SDK 로 만들지 않는다.** `claude -p` (구독 로그인) 만 쓴다.
  `reviewer.subscription_env()` 가 `ANTHROPIC_API_KEY` 를 지우는 것은 의도된 동작이다.
- 리뷰 호출은 `--tools ""` 로 도구 없이 실행한다 — 논문 텍스트는 신뢰할 수 없는 입력이다.
- arXiv 요청은 3초 간격을 지킨다 (`arxiv.REQUEST_INTERVAL`).
- 429 를 받으면 `Retry-After` 동안 프로세스 전체가 arXiv 에 요청하지 않는다 (`_cooldown_until`).
  제한을 피하려고 User-Agent 를 바꾸거나 우회하지 않는다. 웹은 기다리지 않고 남은 시간을 돌려준다.
- 리뷰 서술은 한국어, 기술 용어는 원문(영어) 그대로.
- 핵심 경로는 표준 라이브러리만 쓴다. 선택 의존성(truststore, pymupdf)은 없어도 동작해야 한다.
- 리뷰 전 `auth.check()` 로 구독 로그인을 확인한다. 구독이 아니면 리뷰를 시작하지 않는다.
- 본문이 `max_chars` 에서 잘리면 프롬프트에 알린다 — 안 알리면 모델이 잘린 뒷부분을 '본문에 없다'고 단정한다.
- 리뷰 로직은 `service.py` 한 곳에 둔다. CLI 와 웹이 각자 구현하면 결과물이 갈라진다.
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
