"""논문 본문 텍스트 (선택). PyMuPDF 가 없거나 실패하면 None → 초록만으로 리뷰."""
from __future__ import annotations

import re
import sys
import urllib.request

from .arxiv import USER_AGENT

_REFERENCES_RE = re.compile(r"\n\s*(references|bibliography)\s*\n", re.IGNORECASE)


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


def fetch_fulltext(paper: dict, max_chars: int) -> str | None:
    fitz = _pymupdf()
    if fitz is None:
        return None
    try:
        req = urllib.request.Request(paper["pdf_url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        with fitz.open(stream=data, filetype="pdf") as doc:
            text = "\n".join(page.get_text() for page in doc)
    except Exception as e:  # 네트워크·깨진 PDF — 초록으로 계속 진행
        print(f"  본문 추출 실패({e}) → 초록만 사용", file=sys.stderr)
        return None
    text = strip_references(text).strip()
    return text[:max_chars] if len(text) > 500 else None
