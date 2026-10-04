"""Подсчёт баллов теста. Чистые функции без зависимостей от Telegram."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bot.content import Question

MIN_ANSWER = 1
MAX_ANSWER = 5


class AttachmentType(StrEnum):
    SECURE = "secure"
    ANXIOUS = "anxious"
    AVOIDANT = "avoidant"
    DISORGANIZED = "disorganized"


@dataclass(frozen=True, slots=True)
class Result:
    anxiety: int
    avoidance: int
    type: AttachmentType


def item_score(answer: int, *, reverse: bool) -> int:
    if not MIN_ANSWER <= answer <= MAX_ANSWER:
        raise ValueError(f"answer must be in {MIN_ANSWER}..{MAX_ANSWER}, got {answer}")
    return (MIN_ANSWER + MAX_ANSWER - answer) if reverse else answer


def classify(anxiety: int, avoidance: int, threshold: int) -> AttachmentType:
    high_anx = anxiety > threshold
    high_avo = avoidance > threshold
    if high_anx and high_avo:
        return AttachmentType.DISORGANIZED
    if high_anx:
        return AttachmentType.ANXIOUS
    if high_avo:
        return AttachmentType.AVOIDANT
    return AttachmentType.SECURE


def score(questions: Sequence[Question], answers: Sequence[int], threshold: int) -> Result:
    if len(answers) != len(questions):
        raise ValueError(f"expected {len(questions)} answers, got {len(answers)}")
    totals = {"anxiety": 0, "avoidance": 0}
    for question, answer in zip(questions, answers, strict=True):
        totals[question.dimension] += item_score(answer, reverse=question.reverse)
    return Result(
        anxiety=totals["anxiety"],
        avoidance=totals["avoidance"],
        type=classify(totals["anxiety"], totals["avoidance"], threshold),
    )
