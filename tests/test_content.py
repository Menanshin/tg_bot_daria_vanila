import re

from aiogram.types import InlineKeyboardMarkup

from bot.content import load_content
from bot.scoring import AttachmentType
from bot.ui import final_cta_screen, full_screen, gate_screen, question_screen, start_screen

CONTENT = load_content()
TG_LIMIT = 4096
CALLBACK_LIMIT = 64


def test_balanced_scales():
    dims = [q.dimension for q in CONTENT.questions]
    assert dims.count("anxiety") == dims.count("avoidance") == 5
    for dim in ("anxiety", "avoidance"):
        assert any(q.reverse for q in CONTENT.questions if q.dimension == dim)


def test_threshold_is_scale_midpoint():
    per_scale = len(CONTENT.questions) // 2
    assert CONTENT.threshold == per_scale * 3


def test_unique_question_ids():
    ids = [q.id for q in CONTENT.questions]
    assert len(ids) == len(set(ids))


def _all_screens():
    yield start_screen(CONTENT)
    yield CONTENT.t("scale_intro"), InlineKeyboardMarkup(inline_keyboard=[])
    yield final_cta_screen(CONTENT, "someone")
    for i in range(len(CONTENT.questions)):
        yield question_screen(CONTENT, 999_999_999, i)
    for t in AttachmentType:
        yield gate_screen(CONTENT, "@vanillapropsy", "https://t.me/vanillapropsy", t, 25, 25)
        yield full_screen(CONTENT, "@vanillapropsy", "some_bot", t, 25, 25)


CAPTION_LIMIT = 1024


def test_diary_caption_fits():
    assert len(CONTENT.t("diary")) <= CAPTION_LIMIT


def test_media_files_present():
    for t in AttachmentType:
        assert CONTENT.type_image(t).is_file()
    assert CONTENT.diary_path.is_file()


def test_screens_fit_telegram_limits():
    for text, kb in _all_screens():
        assert len(text) <= TG_LIMIT
        for row in kb.inline_keyboard:
            for btn in row:
                if btn.callback_data:
                    assert len(btn.callback_data.encode()) <= CALLBACK_LIMIT


def test_html_tags_are_balanced():
    for text, _ in _all_screens():
        for tag in ("b", "i", "a"):
            opened = len(re.findall(rf"<{tag}[ >]", text))
            closed = text.count(f"</{tag}>")
            assert opened == closed, (tag, text[:80])


def test_no_hard_wrapped_paragraphs():
    """Абзацы должны быть одной строкой: строка с маленькой буквы = сломанный перенос."""
    for text, _ in _all_screens():
        for line in text.splitlines():
            assert not re.match(r"^\s*[а-яёa-z]", line), line


def test_no_unfilled_placeholders():
    for text, _ in _all_screens():
        assert not re.search(r"\{[a-z_]+\}", text), text[:80]
