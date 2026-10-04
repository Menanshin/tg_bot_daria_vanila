import itertools

import pytest

from bot.content import load_content
from bot.scoring import AttachmentType, classify, item_score, score

CONTENT = load_content()
Q = CONTENT.questions
T = CONTENT.threshold


def answers_for(anxiety_raw: int, avoidance_raw: int) -> list[int]:
    """Ответы, дающие заданный «смысловой» балл (1..5) на каждый вопрос шкалы."""
    out = []
    for q in Q:
        target = anxiety_raw if q.dimension == "anxiety" else avoidance_raw
        out.append(6 - target if q.reverse else target)
    return out


@pytest.mark.parametrize(("answer", "expected"), [(1, 5), (2, 4), (3, 3), (4, 2), (5, 1)])
def test_reverse(answer, expected):
    assert item_score(answer, reverse=True) == expected
    assert item_score(answer, reverse=False) == answer


@pytest.mark.parametrize("bad", [0, 6, -1])
def test_item_score_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        item_score(bad, reverse=False)


def test_wrong_number_of_answers():
    with pytest.raises(ValueError):
        score(Q, [3] * (len(Q) - 1), T)


@pytest.mark.parametrize(
    ("anx", "avo", "expected"),
    [
        (1, 1, AttachmentType.SECURE),
        (5, 1, AttachmentType.ANXIOUS),
        (1, 5, AttachmentType.AVOIDANT),
        (5, 5, AttachmentType.DISORGANIZED),
        (4, 2, AttachmentType.ANXIOUS),
        (2, 4, AttachmentType.AVOIDANT),
    ],
)
def test_types(anx, avo, expected):
    assert score(Q, answers_for(anx, avo), T).type == expected


def test_all_neutral_is_not_disorganized():
    """Регресс к исходной методике: все «3» давали самый тяжёлый тип."""
    r = score(Q, [3] * len(Q), T)
    assert (r.anxiety, r.avoidance) == (15, 15)
    assert r.type == AttachmentType.SECURE


def test_acquiescence_does_not_max_out():
    """Ответ «5» на всё не должен давать максимум: в каждой шкале есть обратные вопросы."""
    r = score(Q, [5] * len(Q), T)
    assert r.anxiety < 25
    assert r.avoidance < 25


def test_ranges_over_all_extreme_combinations():
    for combo in itertools.product([1, 5], repeat=len(Q)):
        r = score(Q, list(combo), T)
        assert 5 <= r.anxiety <= 25
        assert 5 <= r.avoidance <= 25


def test_threshold_boundary():
    assert classify(T, T, T) == AttachmentType.SECURE
    assert classify(T + 1, T, T) == AttachmentType.ANXIOUS
    assert classify(T, T + 1, T) == AttachmentType.AVOIDANT
    assert classify(T + 1, T + 1, T) == AttachmentType.DISORGANIZED
