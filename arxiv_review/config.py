"""설정 로딩. config.json 의 토픽 하나 = arXiv 카테고리 + 주제 키워드."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# cs.CL, stat.ML, hep-th, math.AG, physics.comp-ph ...
CATEGORY_RE = re.compile(r"^[a-z][a-z\-]*(\.[A-Za-z][A-Za-z\-]*)?$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")


class ConfigError(ValueError):
    pass


@dataclass
class Topic:
    name: str
    categories: list[str]
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    primary_category_only: bool = False
    days: int = 7
    max_papers: int = 10

    def validate(self) -> "Topic":
        if not TOPIC_RE.match(self.name):
            raise ConfigError(f"토픽 이름은 영문·숫자·-·_ 만 가능합니다: {self.name!r}")
        if not self.categories:
            raise ConfigError(f"[{self.name}] categories 가 비어 있습니다 (예: cs.CL)")
        for cat in self.categories:
            if not CATEGORY_RE.match(cat):
                raise ConfigError(f"[{self.name}] arXiv 카테고리 형식이 아닙니다: {cat!r}")
        for kw in self.keywords + self.exclude_keywords:
            if not kw.strip() or '"' in kw:
                raise ConfigError(f"[{self.name}] 키워드는 비어 있거나 따옴표를 포함할 수 없습니다: {kw!r}")
        if self.days < 1 or self.max_papers < 1:
            raise ConfigError(f"[{self.name}] days / max_papers 는 1 이상이어야 합니다")
        return self


@dataclass
class ReviewSettings:
    model: str = "sonnet"
    fulltext: bool = True
    max_chars: int = 120000
    timeout_sec: int = 600


@dataclass
class Config:
    topics: dict[str, Topic]
    review: ReviewSettings
    output_dir: Path


def _as_list(value, key: str, topic: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ConfigError(f"[{topic}] {key} 는 문자열 목록이어야 합니다")


def load_config(path: str | Path | None) -> Config:
    """config.json 을 읽는다. 파일이 없으면 토픽 없는 기본 설정."""
    raw: dict = {}
    base = Path.cwd()
    if path is not None:
        p = Path(path)
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ConfigError(f"{p}: JSON 오류 — {e}") from e
            base = p.resolve().parent
    topics = {}
    for name, body in (raw.get("topics") or {}).items():
        known = {"categories", "keywords", "exclude_keywords",
                 "primary_category_only", "days", "max_papers"}
        unknown = set(body) - known
        if unknown:
            raise ConfigError(f"[{name}] 알 수 없는 설정: {sorted(unknown)}")
        topics[name] = Topic(
            name=name,
            categories=_as_list(body.get("categories"), "categories", name),
            keywords=_as_list(body.get("keywords"), "keywords", name),
            exclude_keywords=_as_list(body.get("exclude_keywords"), "exclude_keywords", name),
            primary_category_only=bool(body.get("primary_category_only", False)),
            days=int(body.get("days", 7)),
            max_papers=int(body.get("max_papers", 10)),
        ).validate()
    r = raw.get("review") or {}
    review = ReviewSettings(
        model=str(r.get("model", "sonnet")),
        fulltext=bool(r.get("fulltext", True)),
        max_chars=int(r.get("max_chars", 120000)),
        timeout_sec=int(r.get("timeout_sec", 600)),
    )
    out = Path(raw.get("output_dir", "output"))
    if not out.is_absolute():
        out = base / out
    return Config(topics=topics, review=review, output_dir=out)


def topic_to_dict(topic: Topic) -> dict:
    return {
        "categories": topic.categories,
        "keywords": topic.keywords,
        "exclude_keywords": topic.exclude_keywords,
        "primary_category_only": topic.primary_category_only,
        "days": topic.days,
        "max_papers": topic.max_papers,
    }


def _read_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: JSON 오류 — {e}") from e


def _write_raw(path: Path, raw: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def save_topic(path: str | Path, topic: Topic) -> None:
    """config.json 에 토픽을 추가/교체한다. 파일이 없으면 만든다. 다른 설정은 보존."""
    path = Path(path)
    raw = _read_raw(path)
    raw.setdefault("topics", {})[topic.validate().name] = topic_to_dict(topic)
    _write_raw(path, raw)


def delete_topic(path: str | Path, name: str) -> bool:
    path = Path(path)
    raw = _read_raw(path)
    if name not in (raw.get("topics") or {}):
        return False
    del raw["topics"][name]
    _write_raw(path, raw)
    return True
