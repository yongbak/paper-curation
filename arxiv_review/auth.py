"""claude CLI 로그인 상태 확인 — 리뷰가 Pro/Max 구독으로 처리되는지 미리 본다.

리뷰는 reviewer.subscription_env() 환경(ANTHROPIC_API_KEY 제거)에서 돌므로,
상태 확인도 같은 환경에서 해야 실제 리뷰 호출과 같은 결과가 나온다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass

from .reviewer import subscription_env

_SUBSCRIPTION_RE = re.compile(r"claude\s+(pro|max|team|enterprise)", re.IGNORECASE)


@dataclass
class LoginStatus:
    installed: bool
    logged_in: bool = False
    method: str = ""          # claude 가 표시하는 'Login method' 그대로
    subscription: bool = False

    def problem(self) -> str | None:
        """리뷰를 시작하면 안 되는 이유. 문제가 없으면 None."""
        if not self.installed:
            return ("`claude` 명령을 찾을 수 없습니다. Claude Code 를 먼저 설치하세요:\n"
                    "    npm install -g @anthropic-ai/claude-code")
        if not self.logged_in:
            return ("Claude 에 로그인돼 있지 않습니다. 다음 명령으로 Pro 계정에 로그인하세요:\n"
                    "    python -m arxiv_review login")
        if not self.subscription:
            return (f"현재 로그인 방식이 구독으로 확인되지 않습니다 (Login method: "
                    f"{self.method or '알 수 없음'}).\n"
                    "  이대로 리뷰하면 API 요금이 나갈 수 있습니다. 구독 계정으로 다시 로그인하세요:\n"
                    "    claude auth logout\n"
                    "    python -m arxiv_review login\n"
                    "  구독 로그인이 맞는데 이 검사가 틀렸다면 --skip-login-check 로 건너뛸 수 있습니다.")
        return None


def _run(args: list[str], run) -> subprocess.CompletedProcess:
    return run(args, capture_output=True, text=True, timeout=60, env=subscription_env())


def check(run=subprocess.run, which=shutil.which) -> LoginStatus:
    claude_bin = which("claude")
    if not claude_bin:
        return LoginStatus(installed=False)
    try:
        info = json.loads(_run([claude_bin, "auth", "status", "--json"], run).stdout or "{}")
    except (json.JSONDecodeError, subprocess.SubprocessError, OSError):
        info = {}
    status = LoginStatus(installed=True, logged_in=bool(info.get("loggedIn")))
    if not status.logged_in:
        return status
    try:
        text = _run([claude_bin, "auth", "status", "--text"], run).stdout or ""
    except (subprocess.SubprocessError, OSError):
        text = ""
    m = re.search(r"Login method:\s*(.+)", text)
    status.method = m.group(1).strip() if m else str(info.get("authMethod", ""))
    status.subscription = bool(
        info.get("subscriptionType") or _SUBSCRIPTION_RE.search(status.method))
    return status


def login(run=subprocess.run, which=shutil.which) -> int:
    """`claude auth login --claudeai` 를 그대로 띄운다 (브라우저 로그인)."""
    claude_bin = which("claude")
    if not claude_bin:
        print(LoginStatus(installed=False).problem())
        return 1
    return run([claude_bin, "auth", "login", "--claudeai"], env=subscription_env()).returncode
