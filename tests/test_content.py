import re

from aiogram.types import InlineKeyboardMarkup

from bot.content import load_content
from bot.scoring import AttachmentType
from bot.ui import (
    about_screen,
    final_cta_screen,
    full_screen,
    gate_screen,
    question_screen,
    quiz_intro_text,
    reviews_screen,
    start_screen,
)

CHANNEL, CHANNEL_URL = "@vanillapropsy", "https://t.me/vanillapropsy"

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
    yield quiz_intro_text(CONTENT), InlineKeyboardMarkup(inline_keyboard=[])
    yield about_screen(CONTENT, CHANNEL_URL, booking=True)
    yield final_cta_screen(CONTENT, "someone", CHANNEL_URL)
    for i in range(len(CONTENT.questions)):
        yield question_screen(CONTENT, 999_999_999, i)
    for t in AttachmentType:
        yield gate_screen(CONTENT, "@vanillapropsy", "https://t.me/vanillapropsy", t, 25, 25)
        yield full_screen(CONTENT, CHANNEL, CHANNEL_URL, "some_bot", t, 25, 25)


CAPTION_LIMIT = 1024


def test_diary_caption_fits():
    assert len(CONTENT.t("diary")) <= CAPTION_LIMIT
    assert len(reviews_screen(CONTENT, CHANNEL_URL)[0]) <= CAPTION_LIMIT


def test_media_files_present():
    for t in AttachmentType:
        assert CONTENT.type_image(t).is_file()
    assert CONTENT.diary_path.is_file()
    assert CONTENT.reviews_path.is_file()


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


def test_start_screen_has_author_signature():
    text, _ = start_screen(CONTENT)
    assert text.rstrip().endswith('menanshin.com</a></i>')
    assert 'href="https://t.me/menanshin"' in text


def test_start_screen_buttons():
    from bot.booking_ui import BookMenuCb
    from bot.ui import ABOUT, MenuCb, StartCb

    about = MenuCb(section=ABOUT).pack()

    _, kb = start_screen(CONTENT)
    assert [[b.callback_data for b in row] for row in kb.inline_keyboard] == [
        [StartCb().pack()], [about],
    ]

    _, kb = start_screen(CONTENT, booking=True)
    test_btn, book_btn, about_btn = (row[0] for row in kb.inline_keyboard)
    assert test_btn.text == CONTENT.t("start_button")
    assert test_btn.callback_data == StartCb().pack()
    assert book_btn.text == CONTENT.t("book_button")
    assert book_btn.callback_data == BookMenuCb().pack()
    assert about_btn.callback_data == about

    # Запись выключена, но есть CONTACT — кнопка ведёт в личку Дарьи.
    _, kb = start_screen(CONTENT, contact="dasha")
    assert kb.inline_keyboard[1][0].url == "https://t.me/dasha"


def _urls(kb: InlineKeyboardMarkup) -> list[str]:
    return [btn.url for row in kb.inline_keyboard for btn in row if btn.url]


def test_subscribe_button_everywhere_it_should_be():
    from bot.booking_ui import subscribe_keyboard

    screens = [
        about_screen(CONTENT, CHANNEL_URL),
        about_screen(CONTENT, CHANNEL_URL, booking=True),
        reviews_screen(CONTENT, CHANNEL_URL, booking=True),
        final_cta_screen(CONTENT, "someone", CHANNEL_URL, booking=True),
        final_cta_screen(CONTENT, "", CHANNEL_URL),
        full_screen(CONTENT, CHANNEL, CHANNEL_URL, "some_bot", AttachmentType.SECURE, 5, 5),
        ("", subscribe_keyboard(CONTENT, CHANNEL_URL)),
    ]
    for _, kb in screens:
        assert CHANNEL_URL in _urls(kb)


def test_about_has_booking_and_reviews():
    from bot.booking_ui import BookMenuCb
    from bot.ui import REVIEWS, MenuCb

    _, kb = about_screen(CONTENT, CHANNEL_URL, booking=True)
    data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert BookMenuCb().pack() in data
    assert MenuCb(section=REVIEWS).pack() in data
