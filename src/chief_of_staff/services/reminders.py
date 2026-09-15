"""Plan, persist, claim, and send deadline reminders. Postgres is the source of truth."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from loguru import logger
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief_of_staff.infrastructure.database.orm.project import ProjectRow
from chief_of_staff.infrastructure.database.orm.reminder import TaskReminderRow
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.orm.user import UserRow
from chief_of_staff.models.reminder import ReminderType
from chief_of_staff.models.task import OPEN_TASK_STATUSES
from chief_of_staff.services.clock import KYIV, Clock
from chief_of_staff.services.reminder_messages import format_reminder_message
from chief_of_staff.services.reminder_policy import ReminderSpec, planned_reminders, to_utc

DEFAULT_CLAIM_LEASE = timedelta(minutes=5)


class ReminderNotifier(Protocol):
    async def send_message(self, chat_id: int, text: str) -> None: ...


@dataclass(frozen=True)
class ClaimedReminder:
    reminder_id: UUID
    chat_id: int
    text: str


class ReminderService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession]
        | Callable[[], async_sessionmaker[AsyncSession]],
        clock: Clock,
        notifier: ReminderNotifier,
        *,
        claim_lease: timedelta = DEFAULT_CLAIM_LEASE,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._notifier = notifier
        self._claim_lease = claim_lease

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        factory = self._session_factory
        if isinstance(factory, async_sessionmaker):
            return factory
        return factory()

    async def dispatch_due_reminders(self) -> int:
        """Claim due reminders, send Telegram, then mark sent. Returns send count."""
        claimed = await self.claim_due_reminders()
        sent = 0
        now = to_utc(self._clock.now())
        for item in claimed:
            try:
                await self._notifier.send_message(item.chat_id, item.text)
            except Exception:
                logger.exception(
                    "Reminder send failed reminder_id={rid}",
                    rid=str(item.reminder_id),
                )
                await self._release_claim(item.reminder_id)
                continue
            await self._mark_sent(item.reminder_id, now)
            sent += 1
        return sent

    async def claim_due_reminders(self) -> list[ClaimedReminder]:
        """Atomically lease unsent due reminders. Does not set sent_at."""
        now = to_utc(self._clock.now())
        claimed: list[ClaimedReminder] = []
        async with self._sessions()() as session:
            async with session.begin():
                sql_now = self._sql_time(session, now)
                await self._insert_due_reminders(session, now)
                pending = (
                    await session.execute(
                        select(TaskReminderRow, TaskRow, UserRow, ProjectRow)
                        .join(TaskRow, TaskReminderRow.task_id == TaskRow.id)
                        .join(UserRow, TaskRow.owner_user_id == UserRow.id)
                        .join(ProjectRow, TaskRow.project_id == ProjectRow.id)
                        .where(
                            TaskReminderRow.sent_at.is_(None),
                            TaskReminderRow.scheduled_for <= sql_now,
                            TaskRow.status.in_(OPEN_TASK_STATUSES),
                            UserRow.telegram_chat_id.is_not(None),
                            or_(
                                TaskReminderRow.claimed_at.is_(None),
                                TaskReminderRow.lease_expires_at.is_(None),
                                TaskReminderRow.lease_expires_at <= sql_now,
                            ),
                        )
                    )
                ).all()
                for reminder, task, user, project in pending:
                    if not await self._claim(session, reminder.id, now):
                        continue
                    claimed.append(
                        ClaimedReminder(
                            reminder_id=reminder.id,
                            chat_id=int(user.telegram_chat_id),  # type: ignore[arg-type]
                            text=format_reminder_message(
                                reminder_type=ReminderType(reminder.reminder_type),
                                title=task.title,
                                project=project.name,
                                deadline=task.deadline,
                                estimated_minutes=task.estimated_minutes,
                            ),
                        )
                    )
        return claimed

    async def _insert_due_reminders(self, session: AsyncSession, now: datetime) -> None:
        rows = await session.execute(
            select(TaskRow, UserRow, ProjectRow)
            .join(UserRow, TaskRow.owner_user_id == UserRow.id)
            .join(ProjectRow, TaskRow.project_id == ProjectRow.id)
            .where(
                TaskRow.status.in_(OPEN_TASK_STATUSES),
                UserRow.telegram_chat_id.is_not(None),
            )
        )
        for task, _user, _project in rows.all():
            created_at = task.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=KYIV)
            specs = planned_reminders(
                deadline=task.deadline,
                status=task.status,
                created_at=created_at,
                now=now,
            )
            for spec in specs:
                await self._insert_spec(session, task.id, spec)

    async def _insert_spec(
        self,
        session: AsyncSession,
        task_id: UUID,
        spec: ReminderSpec,
    ) -> None:
        dialect = session.get_bind().dialect.name
        insert = sqlite_insert if dialect == "sqlite" else pg_insert
        stmt = insert(TaskReminderRow).values(
            task_id=task_id,
            reminder_type=spec.reminder_type.value,
            occurrence_date=spec.occurrence_date,
            scheduled_for=spec.scheduled_for,
        ).on_conflict_do_nothing(
            index_elements=["task_id", "reminder_type", "occurrence_date"],
        )
        await session.execute(stmt)

    def _sql_time(self, session: AsyncSession, moment: datetime) -> datetime:
        utc = to_utc(moment)
        if session.get_bind().dialect.name == "sqlite":
            return utc.replace(tzinfo=None)
        return utc

    async def _claim(self, session: AsyncSession, reminder_id: UUID, now: datetime) -> bool:
        sql_now = self._sql_time(session, now)
        lease_end = self._sql_time(session, now + self._claim_lease)
        result = await session.execute(
            update(TaskReminderRow)
            .where(
                TaskReminderRow.id == reminder_id,
                TaskReminderRow.sent_at.is_(None),
                or_(
                    TaskReminderRow.claimed_at.is_(None),
                    TaskReminderRow.lease_expires_at.is_(None),
                    TaskReminderRow.lease_expires_at <= sql_now,
                ),
            )
            .values(claimed_at=sql_now, lease_expires_at=lease_end)
            .returning(TaskReminderRow.id)
        )
        return result.scalar_one_or_none() is not None

    async def _mark_sent(self, reminder_id: UUID, now: datetime) -> None:
        async with self._sessions()() as session:
            async with session.begin():
                await session.execute(
                    update(TaskReminderRow)
                    .where(
                        TaskReminderRow.id == reminder_id,
                        TaskReminderRow.sent_at.is_(None),
                    )
                    .values(sent_at=self._sql_time(session, now))
                )

    async def _release_claim(self, reminder_id: UUID) -> None:
        async with self._sessions()() as session:
            async with session.begin():
                await session.execute(
                    update(TaskReminderRow)
                    .where(
                        TaskReminderRow.id == reminder_id,
                        TaskReminderRow.sent_at.is_(None),
                    )
                    .values(claimed_at=None, lease_expires_at=None)
                )
