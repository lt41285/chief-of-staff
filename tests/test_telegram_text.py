from datetime import date, datetime, timezone
from uuid import uuid4

from telegram import Chat, Message, Update
from telegram.error import Forbidden

from chief_of_staff.bot.handlers.errors import TECHNICAL_ERROR, on_application_error
from chief_of_staff.bot.telegram_text import (
    TELEGRAM_MESSAGE_LIMIT,
    reply_telegram_text,
    split_telegram_text,
)
from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.models.task import Importance, Urgency
from chief_of_staff.services.task_query_format import format_all_tasks_grouped


def test_short_text_stays_one_message() -> None:
    assert split_telegram_text("коротко") == ["коротко"]


def test_split_prefers_paragraph_breaks() -> None:
    first = "A" * 100
    second = "B" * 100
    chunks = split_telegram_text(f"{first}\n\n{second}", limit=150)
    assert chunks == [first, second]
    assert all(len(chunk) <= 150 for chunk in chunks)


def test_hard_cut_when_no_separators() -> None:
    text = "x" * 50
    chunks = split_telegram_text(text, limit=20)
    assert "".join(chunks) == text
    assert all(len(chunk) <= 20 for chunk in chunks)


def test_fat_task_list_fits_telegram_chunks() -> None:
    tasks = [
        PlanCandidate(
            id=uuid4(),
            title=f"Дуже довга назва задачі номер {index} " + ("деталі " * 20),
            project="Фундація",
            people=("Андрій Боровець",),
            deadline=date(2026, 9, 30),
            importance=Importance.HIGH,
            urgency=Urgency.URGENT,
            estimated_minutes=45,
            desired_outcome="Довгий очікуваний результат " + ("текст " * 15),
            status="inbox",
        )
        for index in range(20)
    ]
    text = format_all_tasks_grouped([("Фундація", tasks)], total=20, today=date(2026, 9, 23))
    assert len(text) > TELEGRAM_MESSAGE_LIMIT
    chunks = split_telegram_text(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_MESSAGE_LIMIT for chunk in chunks)
    assert "Дуже довга назва задачі номер 0" in chunks[0]
    assert "Дуже довга назва задачі номер 19" in chunks[-1]


async def test_reply_sends_chunks_and_markup_on_last() -> None:
    sent: list[tuple[str, object]] = []

    async def fake_reply(text: str, **kwargs: object) -> None:
        sent.append((text, kwargs.get("reply_markup")))

    await reply_telegram_text(
        fake_reply,
        "один\n\nдва",
        reply_markup="kb",
        limit=5,
    )
    assert [text for text, _ in sent] == ["один", "два"]
    assert sent[0][1] is None
    assert sent[1][1] == "kb"


def _update_with_chat(chat_id: int = 42) -> Update:
    chat = Chat(chat_id, "private")
    message = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat)
    return Update(update_id=1, message=message)


async def test_error_handler_notifies_user() -> None:
    sent: list[tuple[int, str]] = []

    class Bot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    class Context:
        error = RuntimeError("Message is too long")
        bot = Bot()

    await on_application_error(_update_with_chat(), Context())  # type: ignore[arg-type]
    assert sent == [(42, TECHNICAL_ERROR)]


async def test_error_handler_skips_forbidden() -> None:
    sent: list[str] = []

    class Bot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append(text)

    class Context:
        error = Forbidden("blocked")
        bot = Bot()

    await on_application_error(_update_with_chat(), Context())  # type: ignore[arg-type]
    assert sent == []
