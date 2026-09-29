"""Close or postpone a task by replying to the bot message that shows it."""

from collections.abc import AsyncIterator
from datetime import date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.reminder import ReminderType
from chief_of_staff.models.utterance_intent import RouterKind, UtteranceInterpretation
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.conversation_context import InMemoryConversationStore
from chief_of_staff.services.lifecycle_format import ALREADY_DONE
from chief_of_staff.services.lifecycle_session import InMemoryLifecycleStore
from chief_of_staff.services.reminder_messages import format_reminder_message
from chief_of_staff.services.task_intake import TaskIntakeService
from chief_of_staff.services.task_lifecycle import LifecycleKind, TaskLifecycleService
from chief_of_staff.services.task_query import TaskQueryService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from tests.test_ai_router import ScriptedRouter
from tests.test_reminders import FrozenClock
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft

DUBKO = "Поговорити з Дубковим про кошти від Сливицького для проєктів цифровізації"
BUDGET = "Підготувати бюджет ради"
TODAY = date(2026, 9, 29)


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest.fixture
def repository(session_factory: async_sessionmaker[AsyncSession]) -> SqlAlchemyTaskRepository:
    return SqlAlchemyTaskRepository(session_factory)


def _clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 9, 29, 17, 52, tzinfo=KYIV))


def _reminder(title: str) -> str:
    return format_reminder_message(
        reminder_type=ReminderType.DUE_TODAY,
        title=title,
        project="Фінанси УКУ",
        deadline=TODAY,
        estimated_minutes=10,
    )


async def _task(repository: SqlAlchemyTaskRepository, title: str) -> object:
    return await persist(
        repository,
        complete_draft(
            task_title=title, project="Фінанси УКУ", people=(), deadline=TODAY,
            estimated_minutes=10,
        ),
        user=1,
        chat=1,
    )


class _Bot:
    def __init__(self, repository: SqlAlchemyTaskRepository, router: ScriptedRouter) -> None:
        self.life = TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())
        self.intake = TaskIntakeService(
            ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository
        )
        self.queries = TaskQueryService(repository)
        self.router = router
        self.context = InMemoryConversationStore(_clock())

    async def say(self, text: str, *, reply_to: str | None = None) -> object:
        return await process_user_utterance(
            self.intake,
            1,
            1,
            text,
            lifecycle=self.life,
            queries=self.queries,
            router=self.router,
            context=self.context,
            reply_to_text=reply_to,
        )


async def _status(session_factory: async_sessionmaker[AsyncSession], task_id: object) -> str:
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        return row.status


async def test_reply_to_reminder_closes_that_task(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The exact screenshot chain: reminder → reply «Це завдання я виконав»."""
    task_id = await _task(repository, DUBKO)
    await _task(repository, BUDGET)
    bot = _Bot(repository, ScriptedRouter([]))
    asked = await bot.say("Це завдання я виконав", reply_to=_reminder(DUBKO))
    assert asked.kind == LifecycleKind.ASK_CONFIRM
    assert asked.show_confirm_buttons
    assert DUBKO in asked.text
    assert BUDGET not in asked.text
    await bot.life.confirm(1, 1)
    assert await _status(session_factory, task_id) == "done"


async def test_same_text_without_reply_is_not_bound(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, DUBKO)
    await _task(repository, BUDGET)
    bot = _Bot(repository, ScriptedRouter([]))
    reply = await bot.say("Це завдання я виконав")
    assert not (reply.kind == LifecycleKind.ASK_CONFIRM and DUBKO in reply.text)


async def test_reply_with_router_verb_closes_task(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, DUBKO)
    router = ScriptedRouter([UtteranceInterpretation(kind=RouterKind.COMPLETE_STATEMENT)])
    bot = _Bot(repository, router)
    asked = await bot.say("Закрив", reply_to=_reminder(DUBKO))
    assert asked.kind == LifecycleKind.ASK_CONFIRM
    assert DUBKO in asked.text


async def test_reply_postpones_with_parsed_deadline(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, DUBKO)
    router = ScriptedRouter([UtteranceInterpretation(kind=RouterKind.POSTPONE_TASK)])
    bot = _Bot(repository, router)
    asked = await bot.say("Перенеси на 3 жовтня", reply_to=_reminder(DUBKO))
    assert asked.kind == LifecycleKind.ASK_CONFIRM
    assert DUBKO in asked.text
    assert "3 жовтня" in asked.text


async def test_reply_postpone_without_date_asks_for_it(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, DUBKO)
    router = ScriptedRouter([UtteranceInterpretation(kind=RouterKind.POSTPONE_TASK)])
    bot = _Bot(repository, router)
    asked = await bot.say("Перенеси", reply_to=_reminder(DUBKO))
    assert asked.kind == LifecycleKind.ASK_DEADLINE


async def test_reply_to_list_asks_which_of_listed_tasks(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, DUBKO)
    await _task(repository, BUDGET)
    await _task(repository, "Надіслати звіт Сумі")
    listing = f"📁 Фінанси УКУ\n\n1. {DUBKO}\n2. {BUDGET}"
    bot = _Bot(repository, ScriptedRouter([]))
    asked = await bot.say("Виконано", reply_to=listing)
    assert asked.kind == LifecycleKind.ASK_WHICH
    assert asked.choice_count == 2
    assert "Надіслати звіт Сумі" not in asked.text


async def test_reply_to_already_done_task(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, DUBKO)
    bot = _Bot(repository, ScriptedRouter([]))
    await bot.say("Це завдання я виконав", reply_to=_reminder(DUBKO))
    await bot.life.confirm(1, 1)
    bot.life.cancel(1, 1)
    again = await bot.say("Це завдання я виконав", reply_to=_reminder(DUBKO))
    assert again.text == ALREADY_DONE


async def test_yes_to_open_card_stays_with_the_card(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, DUBKO)
    router = ScriptedRouter([UtteranceInterpretation(kind=RouterKind.CONTINUE_PENDING)])
    bot = _Bot(repository, router)
    card = await bot.say("Це завдання я виконав", reply_to=_reminder(DUBKO))
    await bot.say("так", reply_to=card.text)
    assert await _status(session_factory, task_id) == "done"
