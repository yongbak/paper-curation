"""arXiv 연결용 HTTPS 설정.

Python 의 urllib 은 운영체제 인증서 저장소(macOS 키체인, Windows 인증서 저장소)를
보지 않고 OpenSSL 기본 경로만 본다. 그래서 다음 두 경우에 CERTIFICATE_VERIFY_FAILED 가 난다.
- macOS 에서 python.org 설치판을 쓰고 'Install Certificates.command' 를 실행하지 않은 경우
- 회사·기관 네트워크가 보안 장비로 HTTPS 를 검사해, 그 장비의 인증서가 OS 에만 설치된 경우

인증서 목록은 아래 순서로 고른다.
1. SSL_CERT_FILE / SSL_CERT_DIR 환경변수 — 사용자가 직접 지정했으면 그대로 따른다
2. truststore (설치돼 있으면) — 운영체제 인증서 저장소를 쓴다. 두 경우 모두 해결된다
3. certifi (설치돼 있으면) — Mozilla 인증서 묶음. 1번 경우만 해결된다
4. Python 기본값
"""
from __future__ import annotations

import os
import ssl
import urllib.error
from functools import lru_cache

CERT_HELP = (
    "arXiv 연결의 보안 인증서를 확인하지 못했습니다 (CERTIFICATE_VERIFY_FAILED).\n"
    "  이 컴퓨터의 Python 이 신뢰할 인증서 목록을 찾지 못한 경우입니다. arXiv 가 막은 것이 아닙니다.\n"
    "  해결 (권장): 운영체제 인증서 저장소를 쓰도록 설치한 뒤 다시 실행하세요\n"
    "      pip install truststore\n"
    "  macOS 에서 python.org 설치판을 쓴다면 다음으로도 해결됩니다\n"
    "      open \"/Applications/Python 3.X/Install Certificates.command\"   (3.X 는 내 버전)\n"
    "  회사·기관 네트워크라면 보안 장비 인증서 때문일 수 있습니다. truststore 로 대부분 해결됩니다."
)


class ArxivUnavailable(OSError):
    """arXiv 에 닿지 못한 이유를 사용자에게 그대로 보여 줄 수 있는 오류."""


class CertificateProblem(ArxivUnavailable):
    pass


def trust_source() -> str:
    """ssl_context() 가 어떤 인증서 목록을 쓰는지 (status 표시용)."""
    return _build()[1]


def ssl_context() -> ssl.SSLContext:
    return _build()[0]


@lru_cache(maxsize=1)
def _build():
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context(), "SSL_CERT_FILE 환경변수"
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), "운영체제 인증서 저장소 (truststore)"
    except ImportError:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where()), "certifi 인증서 묶음"
    except ImportError:
        pass
    return ssl.create_default_context(), "Python 기본 인증서 경로"


def is_certificate_error(e: BaseException) -> bool:
    if isinstance(e, ssl.SSLCertVerificationError):
        return True
    return isinstance(e, urllib.error.URLError) and isinstance(
        getattr(e, "reason", None), ssl.SSLCertVerificationError)
