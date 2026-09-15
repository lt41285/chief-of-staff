import asyncio
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.reminder import TaskReminderRow
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.reminder import ReminderType
from chief_of_staff.models.task import Importance, Urgency
from chief_of_staff.services.clock import Clock
from chief_of_staff.services.reminder_policy import at_kyiv
from chief_of_staff.services.reminders import ReminderService
from tests.test_task_persistence import persist
from tests.test_task_validation import complete_draft


class FrozenClock(Clock):
    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.fixture
def repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> SqlAlchemyTaskRepository:
    return SqlAlchemyTaskRepository(
        session_factory,
        clock=FrozenClock(at_kyiv(date(2026, 8, 1), 12, 0)),
    )


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime,
    notifier: object,
    *,
    claim_lease: timedelta = timedelta(minutes=5),
) -> ReminderService:
    return ReminderService(
        session_factory,
        FrozenClock(now),
        notifier,
        claim_lease=claim_lease,
    )


async def test_high_urgent_gets_day_before_then_due_today(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(deadline=date(2026, 9, 2)),
        user=7,
        chat=777,
    )
    notifier = RecordingNotifier()
    first = await _service(
        session_factory, at_kyiv(date(2026, 9, 1), 18, 30), notifier
    ).dispatch_due_reminders()
    assert first == 1
    assert notifier.messages[0][0] == 777
    assert notifier.messages[0][1].startswith("⏰ Нагадування")
    second = await _service(
        session_factory, at_kyiv(date(2026, 9, 2), 9, 15), notifier
    ).dispatch_due_reminders()
    assert second == 1
    assert notifier.messages[1][1].startswith("⚠️ Дедлайн сьогодні")
    assert len(notifier.messages) == 2


async def test_ordinary_task_gets_day_before_and_due_day(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            deadline=date(2026, 9, 2),
            importance=Importance.MEDIUM,
            urgency=Urgency.NOT_URGENT,
        ),
        chat=1,
    )
    notifier = RecordingNotifier()
    evening = await _service(
        session_factory, at_kyiv(date(2026, 9, 1), 18, 30), notifier
    ).dispatch_due_reminders()
    assert evening == 1
    assert notifier.messages[0][1].startswith("⏰ Нагадування")
    morning = await _service(
        session_factory, at_kyiv(date(2026, 9, 2), 9, 15), notifier
    ).dispatch_due_reminders()
    assert morning == 1
    assert notifier.messages[1][1].startswith("⚠️ Дедлайн сьогодні")
    assert not any("High" in message or "Urgent" in message for _, message in notifier.messages)


async def test_done_and_cancelled_ignored(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    done_id = await persist(repository, complete_draft(deadline=date(2026, 9, 2)), user=1, chat=1)
    cancelled_id = await persist(
        repository,
        complete_draft(deadline=date(2026, 9, 2), task_title="Cancel the unused vendor contract now"),
        user=2,
        chat=2,
    )
    async with session_factory() as session:
        async with session.begin():
            done = await session.get(TaskRow, done_id)
            cancelled = await session.get(TaskRow, cancelled_id)
            assert done and cancelled
            done.status = "done"
            cancelled.status = "cancelled"
    notifier = RecordingNotifier()
    sent = await _service(
        session_factory, at_kyiv(date(2026, 9, 2), 9, 15), notifier
    ).dispatch_due_reminders()
    assert sent == 0
    assert notifier.messages == []


async def test_overdue_one_reminder_per_day(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(deadline=date(2026, 8, 30)), chat=3)
    notifier = RecordingNotifier()
    d1 = await _service(
        session_factory, at_kyiv(date(2026, 9, 1), 9, 10), notifier
    ).dispatch_due_reminders()
    d1b = await _service(
        session_factory, at_kyiv(date(2026, 9, 1), 12, 0), notifier
    ).dispatch_due_reminders()
    d2 = await _service(
        session_factory, at_kyiv(date(2026, 9, 2), 9, 10), notifier
    ).dispatch_due_reminders()
    assert d1 == 1
    assert d1b == 0
    assert d2 == 1
    assert all(m[1].startswith("🚨 Прострочено") for m in notifier.messages)
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(TaskReminderRow))
        assert count == 2


async def test_restart_does_not_resend(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(deadline=date(2026, 9, 2)), chat=4)
    now = at_kyiv(date(2026, 9, 2), 9, 20)
    notifier = RecordingNotifier()
    assert await _service(session_factory, now, notifier).dispatch_due_reminders() == 1
    assert await _service(session_factory, now, notifier).dispatch_due_reminders() == 0
    assert len(notifier.messages) == 1


async def test_created_after_scheduled_time_no_stale_spam(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(
        repository,
        complete_draft(deadline=date(2026, 9, 2), importance="high", urgency="urgent"),
        chat=5,
    )
    async with session_factory() as session:
        async with session.begin():
            task = await session.get(TaskRow, task_id)
            assert task
            task.created_at = at_kyiv(date(2026, 9, 2), 15, 0)
    notifier = RecordingNotifier()
    sent = await _service(
        session_factory, at_kyiv(date(2026, 9, 2), 15, 10), notifier
    ).dispatch_due_reminders()
    assert sent == 1
    assert notifier.messages[0][1].startswith("⚠️ Дедлайн сьогодні")
    assert not any(m[1].startswith("⏰") for m in notifier.messages)


async def test_correct_telegram_chat_receives_reminder(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(deadline=date(2026, 9, 2)), user=11, chat=99901)
    await persist(
        repository,
        complete_draft(
            deadline=date(2026, 9, 2),
            task_title="Send the signed budget pack to finance team",
        ),
        user=12,
        chat=99902,
    )
    notifier = RecordingNotifier()
    await _service(
        session_factory, at_kyiv(date(2026, 9, 2), 9, 20), notifier
    ).dispatch_due_reminders()
    chats = sorted(chat for chat, _ in notifier.messages)
    assert chats == [99901, 99902]


async def test_overlapping_workers_cannot_both_claim_same_reminder(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(deadline=date(2026, 9, 2)), chat=8)
    now = at_kyiv(date(2026, 9, 2), 9, 20)
    a = _service(session_factory, now, RecordingNotifier())
    b = _service(session_factory, now, RecordingNotifier())
    claimed = await asyncio.gather(a.claim_due_reminders(), b.claim_due_reminders())
    assert sorted(len(batch) for batch in claimed) == [0, 1]


async def test_overlapping_dispatch_does_not_duplicate(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(deadline=date(2026, 9, 2)), chat=8)
    now = at_kyiv(date(2026, 9, 2), 9, 20)
    notifier = RecordingNotifier()
    a = _service(session_factory, now, notifier)
    b = _service(session_factory, now, notifier)
    sent = await asyncio.gather(a.dispatch_due_reminders(), b.dispatch_due_reminders())
    assert sum(sent) == 1
    assert len(notifier.messages) == 1
    async with session_factory() as session:
        rows = (await session.scalars(select(TaskReminderRow))).all()
        assert len(rows) == 1
        assert rows[0].reminder_type == ReminderType.DUE_TODAY.value
        assert rows[0].sent_at is not None
        assert rows[0].claimed_at is not None


async def _one_reminder(session_factory: async_sessionmaker[AsyncSession]) -> TaskReminderRow:
    async with session_factory() as session:
        row = await session.scalar(select(TaskReminderRow))
        assert row is not None
        return row


class InspectingNotifier:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.messages: list[tuple[int, str]] = []
        self.sent_at_while_sending: datetime | None = None
        self.claimed_at_while_sending: datetime | None = None

    async def send_message(self, chat_id: int, text: str) -> None:
        async with self._session_factory() as session:
            row = await session.scalar(select(TaskReminderRow))
            assert row is not None
            self.sent_at_while_sending = row.sent_at
            self.claimed_at_while_sending = row.claimed_at
        self.messages.append((chat_id, text))


class FailingNotifier:
    async def send_message(self, chat_id: int, text: str) -> None:
        raise RuntimeError("telegram down")


async def test_sent_at_only_after_successful_telegram_send(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(deadline=date(2026, 9, 2)), chat=8)
    now = at_kyiv(date(2026, 9, 2), 9, 20)
    notifier = InspectingNotifier(session_factory)
    sent = await ReminderService(
        session_factory, FrozenClock(now), notifier, claim_lease=timedelta(minutes=5)
    ).dispatch_due_reminders()
    assert sent == 1
    assert notifier.sent_at_while_sending is None
    assert notifier.claimed_at_while_sending is not None
    row = await _one_reminder(session_factory)
    assert row.sent_at is not None


async def test_crash_after_claim_before_send_is_retryable_after_lease(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(deadline=date(2026, 9, 2)), chat=8)
    now = at_kyiv(date(2026, 9, 2), 9, 20)
    lease = timedelta(minutes=5)
    silent = RecordingNotifier()
    service = _service(session_factory, now, silent, claim_lease=lease)
    claimed = await service.claim_due_reminders()
    assert len(claimed) == 1
    row = await _one_reminder(session_factory)
    assert row.sent_at is None
    assert row.claimed_at is not None
    assert row.lease_expires_at is not None

    still_leased = RecordingNotifier()
    assert await _service(session_factory, now, still_leased, claim_lease=lease).dispatch_due_reminders() == 0
    assert still_leased.messages == []
    row = await _one_reminder(session_factory)
    assert row.sent_at is None

    after_lease = RecordingNotifier()
    later = at_kyiv(date(2026, 9, 2), 9, 26)
    assert await _service(session_factory, later, after_lease, claim_lease=lease).dispatch_due_reminders() == 1
    assert len(after_lease.messages) == 1
    row = await _one_reminder(session_factory)
    assert row.sent_at is not None


async def test_stale_claim_becomes_retryable(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(deadline=date(2026, 9, 2)), chat=8)
    now = at_kyiv(date(2026, 9, 2), 9, 20)
    await _service(session_factory, now, RecordingNotifier()).claim_due_reminders()
    async with session_factory() as session:
        async with session.begin():
            row = await session.scalar(select(TaskReminderRow))
            assert row is not None
            row.claimed_at = datetime(2020, 1, 1)
            row.lease_expires_at = datetime(2020, 1, 1)
            row.sent_at = None
    notifier = RecordingNotifier()
    assert await _service(session_factory, now, notifier).dispatch_due_reminders() == 1
    assert len(notifier.messages) == 1
    row = await _one_reminder(session_factory)
    assert row.sent_at is not None


async def test_failed_send_releases_claim_and_does_not_set_sent_at(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(deadline=date(2026, 9, 2)), chat=8)
    now = at_kyiv(date(2026, 9, 2), 9, 20)
    assert await _service(session_factory, now, FailingNotifier()).dispatch_due_reminders() == 0
    row = await _one_reminder(session_factory)
    assert row.sent_at is None
    assert row.claimed_at is None
    assert row.lease_expires_at is None
    notifier = RecordingNotifier()
    assert await _service(session_factory, now, notifier).dispatch_due_reminders() == 1
    assert len(notifier.messages) == 1
