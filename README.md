# arxiv-review

arXiv 의 **특정 카테고리 + 특정 주제**만 검색해서, 각 논문의 **한국어 리뷰**를 만들고
검색 가능한 HTML 한 장으로 보여 주는 작은 도구입니다.

리뷰 생성은 Anthropic API 키가 아니라 **Claude Code CLI(`claude -p`)** 를 호출합니다.
`claude` 가 Pro/Max 구독 계정으로 로그인돼 있으면 사용량은 구독 한도에서 차감되고
API 요금은 발생하지 않습니다.

## 준비

1. Python 3.10 이상
2. [Claude Code](https://claude.com/claude-code) 설치 후 터미널에서 `claude` 실행 → `/login` 으로 Pro 구독 계정 로그인
3. (선택) `pip install -r requirements.txt` — PyMuPDF 를 설치하면 초록이 아니라 **PDF 본문 전체**로 리뷰합니다

```bash
cp config.example.json config.json   # 토픽을 내 관심사로 수정
```

## 사용

```bash
# config.json 의 토픽으로 검색 + 리뷰 + HTML
python -m arxiv_review run --topic llm-agents

# 검색 결과만 미리 보기 (리뷰 없음, 구독 사용량 0)
python -m arxiv_review run --topic llm-agents --dry-run

# config 없이 즉석 검색
python -m arxiv_review run --category cs.CV --keyword "diffusion model" --days 3 --max 5

# 저장된 리뷰로 HTML 만 다시 생성
python -m arxiv_review build --topic llm-agents
```

결과는 `output/<토픽>/` 에 쌓입니다.

| 파일 | 내용 |
|---|---|
| `index.html` | 토픽의 모든 리뷰 — 검색창, 카드, 펼쳐 보는 전체 리뷰 |
| `<arxiv_id>/review.md` | 논문별 한국어 리뷰 (마크다운) |
| `<arxiv_id>/paper.json` | 메타데이터 + 리뷰 원본 (이 파일이 있으면 재실행 때 건너뜀) |

## 토픽 설정

```json
"llm-agents": {
  "categories": ["cs.CL", "cs.AI"],
  "keywords": ["LLM agent", "tool use"],
  "exclude_keywords": ["survey"],
  "primary_category_only": false,
  "days": 7,
  "max_papers": 10
}
```

| 키 | 의미 |
|---|---|
| `categories` | arXiv 카테고리. 여러 개면 OR ([목록](https://arxiv.org/category_taxonomy)) |
| `keywords` | 주제. 제목·초록의 **구문 검색**, 여러 개면 OR. 비우면 카테고리 전체 |
| `exclude_keywords` | 제목·초록에 있으면 제외 |
| `primary_category_only` | cross-list 를 빼고 primary 카테고리가 일치하는 논문만 |
| `days` / `max_papers` | 최근 N일 제출분 중 최신순 최대 M편 |

검색식은 `(카테고리들) AND (키워드들) AND 제출일 범위` 입니다.

## 구독 사용량에 관해

- 논문 1편 = `claude -p` 호출 1회입니다. 한 번에 한 편씩 순차 처리합니다.
- 기본 모델은 `sonnet` 입니다 (`review.model` 또는 `--model` 로 변경).
- 이미 리뷰한 논문은 건너뛰므로 재실행은 새 논문에만 사용량을 씁니다 (`--force` 로 다시 리뷰).
- 구독 한도에 걸리면 그 자리에서 멈추고, 한도가 풀린 뒤 같은 명령을 다시 실행하면 이어집니다.
- 환경변수 `ANTHROPIC_API_KEY` 가 있어도 리뷰 호출에서는 제거하므로 API 과금으로 넘어가지 않습니다.

## 리뷰 형식

Essence · 알려진 것 · 연구 갭 · 왜 중요한가 · 접근법 · 성과 · 방법론 · 독창성 ·
한계와 후속 연구 · 평가(5개 항목 1~5점) · 총평. 서술은 한국어, 기술 용어는 원문 그대로 둡니다.

## 테스트

```bash
python -m unittest discover -s tests
```
