"""리뷰 → review.md, 토픽별 index.html (자기완결, 검색 가능)."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from .arxiv import ARXIV_ID_RE

SECTIONS = [
    ("essence", "Essence"),
    ("known", "알려진 것"),
    ("gap", "연구 갭"),
    ("why", "왜 중요한가"),
    ("approach", "접근법"),
    ("achievement", "성과"),
    ("how", "방법론"),
    ("experiments", "실험 결과"),
    ("originality", "독창성"),
    ("limitation", "한계와 후속 연구"),
]
SCORES = [("novelty", "Novelty"), ("technical", "Technical soundness"),
          ("significance", "Significance"), ("clarity", "Clarity"),
          ("overall", "Overall")]


def paper_dirname(arxiv_id: str) -> str:
    # 웹 요청에서도 들어오는 값이다 — '..' 같은 경로가 디렉터리 이름이 되면 안 된다.
    if not ARXIV_ID_RE.match(arxiv_id):
        raise ValueError(f"arXiv ID 형식이 아닙니다: {arxiv_id!r}")
    return arxiv_id.replace("/", "_")


def basis_label(meta: dict) -> str:
    if meta.get("basis") != "fulltext":
        return "초록"
    sent, total = meta.get("fulltext_chars"), meta.get("fulltext_total_chars")
    if sent and total and sent < total:
        return f"본문 앞 {sent:,}자 (전체 {total:,}자 중 {sent * 100 // total}%)"
    return "본문 전체 (참고문헌 제외)"


def review_markdown(paper: dict, review: dict, meta: dict) -> str:
    lines = [
        "---",
        f"title: {json.dumps(paper['title'], ensure_ascii=False)}",
        f"arxiv_id: {paper['arxiv_id']}",
        f"published: {paper['published']}",
        f"primary_category: {paper['primary_category']}",
        f"review_basis: {meta.get('basis', 'abstract')}",
        f"review_model: {meta.get('model', '')}",
        f"reviewed_at: {meta.get('reviewed_at', '')}",
        "---",
        "",
        f"# {paper['title']}",
        "",
        f"> **저자**: {', '.join(paper['authors'])}  ",
        f"> **arXiv**: [{paper['arxiv_id']}]({paper['abs_url']}) · "
        f"{' · '.join(paper['categories'])} · {paper['published']}  ",
        f"> **리뷰 근거**: {basis_label(meta)}",
        "",
    ]
    if paper.get("abstract"):
        lines += ["## Abstract", "", paper["abstract"], ""]
    for key, label in SECTIONS:
        if review.get(key):
            lines += [f"## {label}", "", review[key], ""]
    lines += ["## 평가", "", "| 항목 | 점수 |", "|---|---|"]
    lines += [f"| {label} | {review[key]}/5 |" for key, label in SCORES]
    lines += ["", f"**총평**: {review['verdict']}", ""]
    return "\n".join(lines)


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


def md_to_html(text: str) -> str:
    """리뷰 필드에 나오는 것만 처리한다: 문단, bullet, 번호 목록, 굵게, 코드."""
    out, list_tag = [], None

    def close():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for line in text.splitlines():
        stripped = line.strip()
        bullet = re.match(r"^[-*]\s+(.*)", stripped)
        numbered = re.match(r"^\d+[.)]\s+(.*)", stripped)
        if bullet or numbered:
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close()
                out.append(f"<{tag}>")
                list_tag = tag
            out.append(f"<li>{_inline((bullet or numbered).group(1))}</li>")
        elif not stripped:
            close()
        elif list_tag and line.startswith((" ", "\t")):  # 목록 항목의 이어지는 줄
            out[-1] = out[-1][:-5] + " " + _inline(stripped) + "</li>"
        else:
            close()
            out.append(f"<p>{_inline(stripped)}</p>")
    close()
    return "\n".join(out)


def load_entries(topic_dir: Path) -> list[dict]:
    """저장된 paper.json 들 (리뷰가 있는 것만), 최신순."""
    entries = []
    for path in topic_dir.glob("*/paper.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if entry.get("review"):
            entries.append(entry)
    entries.sort(key=lambda e: (e["paper"]["published"], e["paper"]["arxiv_id"]),
                 reverse=True)
    return entries


_CSS = """
:root{--bg:#fafaf8;--card:#fff;--fg:#1c1c1a;--muted:#6b6b66;--line:#e4e3de;--accent:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--fg:#ecebe6;
--muted:#9c9b94;--line:#33332f;--accent:#ff8a80}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);line-height:1.65;
font-family:-apple-system,"Pretendard","Noto Sans KR","Malgun Gothic",sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px 80px}
h1{font-size:1.5rem;margin:0 0 4px}
.sub{color:var(--muted);font-size:.9rem;margin-bottom:20px}
#q{width:100%;padding:10px 12px;font-size:1rem;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--fg);margin-bottom:20px}
article{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:18px 20px;margin-bottom:14px}
article h2{font-size:1.08rem;margin:0 0 6px;line-height:1.4}
article h2 a{color:inherit;text-decoration:none}
article h2 a:hover{color:var(--accent)}
.meta{color:var(--muted);font-size:.83rem;margin-bottom:10px}
.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;
padding:0 8px;margin-right:4px;font-size:.78rem}
.overall{border-color:var(--accent);color:var(--accent);font-weight:600}
.essence{margin:0 0 8px}
summary{cursor:pointer;color:var(--accent);font-size:.9rem}
h3{font-size:.95rem;margin:16px 0 4px}
p{margin:4px 0}ul,ol{margin:4px 0;padding-left:22px}
code{font-size:.88em;background:var(--line);padding:1px 4px;border-radius:4px}
.scores{color:var(--muted);font-size:.85rem;margin-top:12px}
"""

_JS = """
const q=document.getElementById('q'),n=document.getElementById('n');
q.addEventListener('input',()=>{const t=q.value.toLowerCase().split(/\\s+/).filter(Boolean);
let c=0;document.querySelectorAll('article').forEach(a=>{
const ok=t.every(w=>a.dataset.text.includes(w));a.hidden=!ok;if(ok)c++;});
n.textContent=c;});
"""


def _article(entry: dict) -> str:
    p, r, m = entry["paper"], entry["review"], entry.get("meta", {})
    e = html.escape
    authors = ", ".join(p["authors"][:6]) + (" 외" if len(p["authors"]) > 6 else "")
    basis = ("본문 일부" if m.get("fulltext_chars", 0) < m.get("fulltext_total_chars", 0)
             else "본문") if m.get("basis") == "fulltext" else "초록"
    search_text = " ".join([p["title"], " ".join(p["authors"]), p["abstract"],
                            *(r.get(k, "") for k, _ in SECTIONS), r["verdict"]]).lower()
    body = "".join(f"<h3>{e(label)}</h3>\n{md_to_html(r[key])}\n"
                   for key, label in SECTIONS[1:] if r.get(key))
    scores = " · ".join(f"{e(label)} {r[key]}/5" for key, label in SCORES)
    return (
        f'<article data-text="{e(search_text, quote=True)}">\n'
        f'<h2><a href="{e(p["abs_url"], quote=True)}" target="_blank" rel="noopener">'
        f'{e(p["title"])}</a></h2>\n'
        f'<div class="meta"><span class="badge overall">Overall {r["overall"]}/5</span>'
        f'<span class="badge">{e(p["primary_category"])}</span>'
        f'<span class="badge">{basis} 기반</span> '
        f'{e(p["published"])} · {e(authors)} · '
        f'<a href="{e(p["pdf_url"], quote=True)}" target="_blank" rel="noopener">PDF</a> · '
        f'<a href="{e(paper_dirname(p["arxiv_id"]), quote=True)}/review.md">review.md</a></div>\n'
        f'<div class="essence">{md_to_html(r["essence"])}</div>\n'
        f'<details><summary>리뷰 전체 보기</summary>\n'
        f'<h3>Abstract</h3>\n<p>{e(p["abstract"])}</p>\n{body}'
        f'<h3>총평</h3>\n{md_to_html(r["verdict"])}\n'
        f'<div class="scores">{scores}</div></details>\n</article>'
    )


def build_index(topic_dir: Path, topic_name: str, description: str = "") -> Path:
    entries = load_entries(topic_dir)
    e = html.escape
    page = (
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<title>{e(topic_name)} — arXiv 리뷰</title>\n<style>{_CSS}</style></head>\n'
        f'<body><main>\n<h1>{e(topic_name)}</h1>\n'
        f'<div class="sub">{e(description)} · 리뷰 <span id="n">{len(entries)}</span>편</div>\n'
        '<input id="q" type="search" placeholder="제목·저자·리뷰 내용 검색" autofocus>\n'
        + "\n".join(_article(en) for en in entries)
        + f'\n</main><script>{_JS}</script></body></html>\n'
    )
    out = topic_dir / "index.html"
    out.write_text(page, encoding="utf-8")
    return out
