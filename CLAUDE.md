# CLAUDE.md

arXiv 특정 카테고리·주제 검색 → 한국어 리뷰 → 검색 가능한 HTML. 사용법은 README.md.

## 구조

| 파일 | 역할 |
|---|---|
| `arxiv_review/config.py` | `config.json` 로딩·검증. 토픽 = 카테고리 + 키워드 |
| `arxiv_review/arxiv.py` | arXiv API 검색식 생성, Atom 파싱, 후처리 필터 |
| `arxiv_review/fulltext.py` | PDF 본문 추출 (PyMuPDF 선택 의존성, 없으면 초록만) |
| `arxiv_review/reviewer.py` | `claude -p` 호출로 구조화된 한국어 리뷰 생성 |
| `arxiv_review/auth.py` | `claude auth status` 로 구독 로그인 확인, `login` |
| `arxiv_review/render.py` | `review.md`, 토픽별 `index.html` |
| `arxiv_review/cli.py` | `login` / `status` / `topic add·list·remove` / `run` / `build` |

## 지켜야 할 것

- **리뷰는 Anthropic API SDK 로 만들지 않는다.** `claude -p` (구독 로그인) 만 쓴다.
  `reviewer.subscription_env()` 가 `ANTHROPIC_API_KEY` 를 지우는 것은 의도된 동작이다.
- 리뷰 호출은 `--tools ""` 로 도구 없이 실행한다 — 논문 텍스트는 신뢰할 수 없는 입력이다.
- arXiv 요청은 3초 간격을 지킨다 (`arxiv.REQUEST_INTERVAL`).
- 리뷰 서술은 한국어, 기술 용어는 원문(영어) 그대로.
- 핵심 경로는 표준 라이브러리만 쓴다. 의존성을 늘리지 않는다.
- 리뷰 전 `auth.check()` 로 구독 로그인을 확인한다. 구독이 아니면 리뷰를 시작하지 않는다.
- 본문이 `max_chars` 에서 잘리면 프롬프트에 알린다 — 안 알리면 모델이 잘린 뒷부분을 '본문에 없다'고 단정한다.
- `paper.json` 은 마지막에 쓴다 — 그 파일의 존재가 "리뷰 완료" 표시다.
- `config.json` 과 `output/` 은 gitignore 대상이다.

테스트: `python -m unittest discover -s tests` (네트워크·claude 호출 없음)
