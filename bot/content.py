"""Загрузка и валидация вопросов и текстов из YAML."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from bot.scoring import AttachmentType

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
DIMENSIONS = ("anxiety", "avoidance")

REQUIRED_TEXTS = (
    "start", "start_button", "diary", "diary_filename", "scale_intro", "question",
    "back_button", "calculating", "gate", "subscribe_button", "check_button",
    "not_subscribed", "full_header", "full_footer", "share_button", "share_text",
    "restart_button", "already_full", "final_cta", "final_cta_button",
    "final_cta_message", "reminder",
)
MEDIA_DIR_NAME = "media"
DIARY_FILE = "diary.pdf"


class ContentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Question:
    id: int
    text: str
    dimension: Literal["anxiety", "avoidance"]
    reverse: bool


@dataclass(frozen=True, slots=True)
class TypeTexts:
    title: str
    description: str
    full: str


@dataclass(frozen=True, slots=True)
class Content:
    questions: tuple[Question, ...]
    threshold: int
    texts: dict[str, str]
    types: dict[AttachmentType, TypeTexts]
    media_dir: Path

    def type_image(self, result_type: AttachmentType) -> Path:
        return self.media_dir / f"{result_type.value}.jpg"

    @property
    def diary_path(self) -> Path:
        return self.media_dir / DIARY_FILE

    def t(self, key: str, **kwargs: Any) -> str:
        return self.texts[key].format(**kwargs).strip()


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ContentError(f"{path.name}: expected a mapping at top level")
    return data


def load_content(content_dir: Path = CONTENT_DIR) -> Content:
    quiz = _load_yaml(content_dir / "quiz.yaml")
    texts_raw = _load_yaml(content_dir / "texts.yaml")

    questions = tuple(
        Question(
            id=int(q["id"]),
            text=str(q["text"]).strip(),
            dimension=q["dimension"],
            reverse=bool(q.get("reverse", False)),
        )
        for q in quiz["questions"]
    )
    for q in questions:
        if q.dimension not in DIMENSIONS:
            raise ContentError(f"question {q.id}: unknown dimension {q.dimension!r}")
    if not questions:
        raise ContentError("quiz.yaml: no questions")
    for dim in DIMENSIONS:
        if not any(q.dimension == dim for q in questions):
            raise ContentError(f"quiz.yaml: no questions for dimension {dim!r}")

    types_raw = texts_raw.pop("types")
    types: dict[AttachmentType, TypeTexts] = {}
    for t in AttachmentType:
        if t.value not in types_raw:
            raise ContentError(f"texts.yaml: missing type {t.value!r}")
        raw = types_raw[t.value]
        types[t] = TypeTexts(
            title=str(raw["title"]).strip(),
            description=str(raw["description"]).strip(),
            full=str(raw["full"]).strip(),
        )

    texts = {k: str(v) for k, v in texts_raw.items()}
    missing = [k for k in REQUIRED_TEXTS if k not in texts]
    if missing:
        raise ContentError(f"texts.yaml: missing keys {missing}")

    media_dir = content_dir / MEDIA_DIR_NAME
    expected = [media_dir / f"{t.value}.jpg" for t in AttachmentType] + [media_dir / DIARY_FILE]
    absent = [p.name for p in expected if not p.is_file()]
    if absent:
        raise ContentError(f"content/{MEDIA_DIR_NAME}: missing files {absent}")

    return Content(
        questions=questions,
        threshold=int(quiz["threshold"]),
        texts=texts,
        types=types,
        media_dir=media_dir,
    )
