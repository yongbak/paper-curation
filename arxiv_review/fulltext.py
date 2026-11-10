"""논문 본문 텍스트 (선택). PyMuPDF 가 없거나 실패하면 None → 초록만으로 리뷰."""
from __future__ import annotations

import re
import sys
import urllib.request
from dataclasses import dataclass

from .arxiv import USER_AGENT

_REFERENCES_RE = re.compile(r"\n\s*(references|bibliography)\s*\n", re.IGNORECASE)


@dataclass
class FullText:
    text: str          # 모델에 보낼 본문 (max_chars 이하)
    total_chars: int   # 참고문헌을 뺀 본문 전체 길이

    @property
    def truncated(self) -> bool:
        return len(self.text) < self.total_chars


def _pymupdf():
    try:
        import pymupdf  # PyMuPDF >= 1.24
        return pymupdf
    except ImportError:
        try:
            import fitz  # 구버전 이름
            return fitz
        except ImportError:
            return None


def pymupdf_available() -> bool:
    return _pymupdf() is not None


def strip_references(text: str) -> str:
    """참고문헌 이후는 리뷰에 쓸모없고 토큰만 먹는다. 마지막 'References' 부터 자른다."""
    matches = list(_REFERENCES_RE.finditer(text))
    if matches and matches[-1].start() > len(text) * 0.4:
        return text[:matches[-1].start()]
    return text


def extract(pdf_bytes: bytes, max_chars: int) -> FullText | None:
    fitz = _pymupdf()
    if fitz is None:
        return None
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)
    text = strip_references(text).strip()
    if len(text) <= 500:  # 스캔본 등 텍스트가 거의 없는 PDF
        return None
    return FullText(text=text[:max_chars], total_chars=len(text))


def fetch_fulltext(paper: dict, max_chars: int) -> FullText | None:
    if not pymupdf_available():
        return None
    try:
        req = urllib.request.Request(paper["pdf_url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        return extract(data, max_chars)
    except Exception as e:  # 네트워크·깨진 PDF — 초록으로 계속 진행
        print(f"  본문 추출 실패({e}) → 초록만 사용", file=sys.stderr)
        return None
