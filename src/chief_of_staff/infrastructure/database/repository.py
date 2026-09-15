"""Persist validated tasks. Reuses projects and people by normalized name."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief_of_staff.infrastructure.database.orm.reminder import TaskReminderRow
from chief_of_staff.infrastructure.database.orm.person import PersonRow
from chief_of_staff.infrastructure.database.orm.person_alias import PersonAliasRow
from chief_of_staff.infrastructure.database.orm.project import ProjectRow
from chief_of_staff.infrastructure.database.orm.project_alias import ProjectAliasRow
from chief_of_staff.infrastructure.database.orm.task import TaskPersonRow, TaskRow
from chief_of_staff.infrastructure.database.orm.user import UserRow
from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.models.task import (
    CLOSED_TASK_STATUSES,
    DEFAULT_IMPORTANCE,
    DEFAULT_URGENCY,
    OPEN_TASK_STATUSES,
    Importance,
    TaskDraft,
    Urgency,
)
from chief_of_staff.services.clock import Clock
from chief_of_staff.services.names import (
    display_project_name,
    normalize_entity_name,
    normalize_project_name,
    projects_look_similar,
)
from chief_of_staff.services.person_match import PersonHit
from chief_of_staff.services.person_resolution import PersonAliasHit, inflection_variant
from chief_of_staff.services.task_validation import is_ready


@dataclass(frozen=True)
class ResolvedProject:
    id: UUID
    name: str
    status: str = "active"


def _resolved(row: ProjectRow) -> ResolvedProject:
    return ResolvedProject(id=row.id, name=row.name, status=row.status)


@dataclass(frozen=True)
class PersonEquivalenceOutcome:
    status: str  # ok | conflict | not_found
    canonical_name: str
    alias: str
    conflict_with: str | None = None
    merged: bool = False


def _task_visible_to_owner(owner_id: UUID):
    """User-owned tasks, plus legacy rows with NULL owner on that user's project."""
    return or_(
        TaskRow.owner_user_id == owner_id,
        and_(TaskRow.owner_user_id.is_(None), ProjectRow.owner_user_id == owner_id),
    )


class TaskRepository(Protocol):
    async def save_validated_task(
        self,
        draft: TaskDraft,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        project_id: UUID | None = None,
    ) -> UUID: ...


class SqlAlchemyTaskRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession]
        | Callable[[], async_sessionmaker[AsyncSession]],
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or Clock()

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        factory = self._session_factory
        if isinstance(factory, async_sessionmaker):
            return factory
        return factory()

    async def save_validated_task(
        self,
        draft: TaskDraft,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        project_id: UUID | None = None,
    ) -> UUID:
        if not is_ready(draft):
            raise ValueError("Cannot persist an incomplete task draft")
        async with self._sessions()() as session:
            async with session.begin():
                return await self._save(
                    session,
                    draft,
                    telegram_user_id=telegram_user_id,
                    telegram_chat_id=telegram_chat_id,
                    project_id=project_id,
                )

    async def _save(
        self,
        session: AsyncSession,
        draft: TaskDraft,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        project_id: UUID | None = None,
    ) -> UUID:
        owner = await self._get_or_create_user(session, telegram_user_id, telegram_chat_id)
        if project_id is not None:
            project = await session.get(ProjectRow, project_id)
            if project is None or project.owner_user_id != owner.id:
                raise ValueError("Project does not belong to this user")
        else:
            project = await self._lookup_project(session, owner.id, draft.project or "")
            if project is None:
                raise ValueError("project_not_found")
        now = self._clock.now()
        task = TaskRow(
            owner_user_id=owner.id,
            project_id=project.id,
            title=draft.task_title or "",
            desired_outcome=draft.desired_outcome or "",
            deadline=draft.deadline,
            importance=(draft.importance or DEFAULT_IMPORTANCE).value,
            urgency=(draft.urgency or DEFAULT_URGENCY).value,
            estimated_minutes=draft.estimated_minutes or 0,
            status="inbox",
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.flush()
        await self._link_people(session, task.id, draft.people, owner.id)
        return task.id

    async def list_open_tasks_for_telegram_user(
        self, telegram_user_id: int
    ) -> list[PlanCandidate]:
        return await self.list_user_tasks(
            telegram_user_id,
            exclude_statuses=(*CLOSED_TASK_STATUSES, "waiting"),
        )

    async def list_user_tasks(
        self,
        telegram_user_id: int,
        *,
        exclude_statuses: Sequence[str] | None = CLOSED_TASK_STATUSES,
        only_statuses: Sequence[str] | None = None,
        include_archived_projects: bool = False,
        project_id: UUID | None = None,
    ) -> list[PlanCandidate]:
        async with self._sessions()() as session:
            owner = await session.scalar(
                select(UserRow).where(UserRow.telegram_id == telegram_user_id)
            )
            if owner is None:
                return []
            query = (
                select(TaskRow, ProjectRow)
                .join(ProjectRow, TaskRow.project_id == ProjectRow.id)
                .where(_task_visible_to_owner(owner.id))
            )
            if not include_archived_projects:
                query = query.where(ProjectRow.status == "active")
            if project_id is not None:
                query = query.where(TaskRow.project_id == project_id)
            if only_statuses is not None:
                query = query.where(TaskRow.status.in_(tuple(only_statuses)))
            elif exclude_statuses:
                query = query.where(TaskRow.status.notin_(tuple(exclude_statuses)))
            else:
                query = query.where(TaskRow.status.in_(OPEN_TASK_STATUSES))
            rows = (await session.execute(query)).all()
            return await self._candidates_from_rows(session, rows)

    async def list_people_for_user(self, telegram_user_id: int) -> list[PersonHit]:
        """People on this user's tasks (any status). Never inserts a person."""
        async with self._sessions()() as session:
            owner = await session.scalar(
                select(UserRow).where(UserRow.telegram_id == telegram_user_id)
            )
            if owner is None:
                return []
            via_people = (
                select(PersonRow.id, PersonRow.name)
                .join(TaskPersonRow, TaskPersonRow.person_id == PersonRow.id)
                .join(TaskRow, TaskRow.id == TaskPersonRow.task_id)
                .join(ProjectRow, TaskRow.project_id == ProjectRow.id)
                .where(_task_visible_to_owner(owner.id))
            )
            via_waiting = (
                select(PersonRow.id, PersonRow.name)
                .join(TaskRow, TaskRow.waiting_for_person_id == PersonRow.id)
                .join(ProjectRow, TaskRow.project_id == ProjectRow.id)
                .where(_task_visible_to_owner(owner.id))
            )
            rows = (await session.execute(via_people)).all()
            rows += (await session.execute(via_waiting)).all()
            unique: dict[UUID, PersonHit] = {}
            for person_id, name in rows:
                unique[person_id] = PersonHit(name=name, person_id=person_id)
            return sorted(unique.values(), key=lambda item: item.name.casefold())

    async def get_user_task(
        self, telegram_user_id: int, task_id: UUID
    ) -> PlanCandidate | None:
        async with self._sessions()() as session:
            row = (
                await session.execute(
                    select(TaskRow, ProjectRow)
                    .join(ProjectRow, TaskRow.project_id == ProjectRow.id)
                    .join(UserRow, TaskRow.owner_user_id == UserRow.id)
                    .where(
                        UserRow.telegram_id == telegram_user_id,
                        TaskRow.id == task_id,
                    )
                )
            ).first()
            if row is None:
                return None
            found = await self._candidates_from_rows(session, [row])
            return found[0] if found else None

    async def complete_user_task(
        self,
        telegram_user_id: int,
        task_id: UUID,
        *,
        completed_at: datetime,
        actual_minutes: int | None = None,
    ) -> Literal["completed", "already_done", "not_found"]:
        async with self._sessions()() as session:
            async with session.begin():
                owner = await session.scalar(
                    select(UserRow).where(UserRow.telegram_id == telegram_user_id)
                )
                if owner is None:
                    return "not_found"
                task = await session.get(TaskRow, task_id)
                if task is None:
                    return "not_found"
                if task.owner_user_id is None:
                    project = await session.get(ProjectRow, task.project_id)
                    if project is None or project.owner_user_id != owner.id:
                        return "not_found"
                    task.owner_user_id = owner.id
                elif task.owner_user_id != owner.id:
                    return "not_found"
                if task.status == "done":
                    return "already_done"
                if task.status == "cancelled":
                    return "not_found"
                task.status = "done"
                task.completed_at = completed_at
                if actual_minutes is not None:
                    task.actual_minutes = actual_minutes
                return "completed"

    async def postpone_user_task(
        self,
        telegram_user_id: int,
        task_id: UUID,
        new_deadline: date,
    ) -> Literal["updated", "not_found"]:
        async with self._sessions()() as session:
            async with session.begin():
                task = await self._owned_open_task(session, telegram_user_id, task_id)
                if task is None:
                    return "not_found"
                if task.deadline != new_deadline:
                    await session.execute(
                        delete(TaskReminderRow).where(
                            TaskReminderRow.task_id == task.id,
                            TaskReminderRow.sent_at.is_(None),
                        )
                    )
                    task.deadline = new_deadline
                return "updated"

    async def set_user_task_waiting(
        self,
        telegram_user_id: int,
        task_id: UUID,
        waiting_for: str | None,
    ) -> Literal["updated", "not_found"]:
        async with self._sessions()() as session:
            async with session.begin():
                task = await self._owned_open_task(session, telegram_user_id, task_id)
                if task is None:
                    return "not_found"
                task.status = "waiting"
                if waiting_for and waiting_for.strip():
                    person = await self._resolve_or_create_person(
                        session, waiting_for, owner_user_id=task.owner_user_id
                    )
                    task.waiting_for_person_id = person.id
                else:
                    task.waiting_for_person_id = None
                return "updated"

    async def resume_user_task(
        self, telegram_user_id: int, task_id: UUID
    ) -> Literal["updated", "not_waiting", "not_found"]:
        async with self._sessions()() as session:
            async with session.begin():
                task = await self._owned_open_task(session, telegram_user_id, task_id)
                if task is None:
                    return "not_found"
                if task.status != "waiting":
                    return "not_waiting"
                task.status = "next"
                task.waiting_for_person_id = None
                return "updated"

    async def _owned_open_task(
        self, session: AsyncSession, telegram_user_id: int, task_id: UUID
    ) -> TaskRow | None:
        owner = await session.scalar(
            select(UserRow).where(UserRow.telegram_id == telegram_user_id)
        )
        if owner is None:
            return None
        task = await session.get(TaskRow, task_id)
        if task is None:
            return None
        if task.owner_user_id is None:
            project = await session.get(ProjectRow, task.project_id)
            if project is None or project.owner_user_id != owner.id:
                return None
            task.owner_user_id = owner.id
        elif task.owner_user_id != owner.id:
            return None
        if task.status in CLOSED_TASK_STATUSES:
            return None
        return task

    async def set_actual_minutes(
        self, telegram_user_id: int, task_id: UUID, minutes: int
    ) -> bool:
        async with self._sessions()() as session:
            async with session.begin():
                owner = await session.scalar(
                    select(UserRow).where(UserRow.telegram_id == telegram_user_id)
                )
                if owner is None:
                    return False
                task = await session.get(TaskRow, task_id)
                if task is None or task.owner_user_id != owner.id or task.status != "done":
                    return False
                task.actual_minutes = minutes
                return True

    async def _candidates_from_rows(
        self,
        session: AsyncSession,
        rows: Sequence[tuple[TaskRow, ProjectRow]],
    ) -> list[PlanCandidate]:
        if not rows:
            return []
        task_ids = [task.id for task, _project in rows]
        people_map: dict[UUID, list[str]] = {task_id: [] for task_id in task_ids}
        people_rows = (
            await session.execute(
                select(TaskPersonRow.task_id, PersonRow.name)
                .join(PersonRow, TaskPersonRow.person_id == PersonRow.id)
                .where(TaskPersonRow.task_id.in_(task_ids))
            )
        ).all()
        for task_id, name in people_rows:
            people_map[task_id].append(name)
        waiting_ids = [
            task.waiting_for_person_id
            for task, _project in rows
            if task.waiting_for_person_id is not None
        ]
        waiting_names: dict[UUID, str] = {}
        if waiting_ids:
            waiting_rows = (
                await session.execute(
                    select(PersonRow.id, PersonRow.name).where(PersonRow.id.in_(waiting_ids))
                )
            ).all()
            waiting_names = {person_id: name for person_id, name in waiting_rows}
        return [
            PlanCandidate(
                id=task.id,
                title=task.title,
                project=project.name,
                people=tuple(people_map[task.id]),
                deadline=task.deadline,
                importance=Importance(task.importance),
                urgency=Urgency(task.urgency),
                estimated_minutes=task.estimated_minutes,
                status=task.status,
                project_id=project.id,
                desired_outcome=task.desired_outcome or "",
                actual_minutes=task.actual_minutes,
                waiting_for=waiting_names.get(task.waiting_for_person_id)
                if task.waiting_for_person_id
                else None,
                created_at=task.created_at,
            )
            for task, project in rows
        ]

    async def mark_tasks_today(
        self, telegram_user_id: int, task_ids: tuple[UUID, ...]
    ) -> None:
        if not task_ids:
            return
        async with self._sessions()() as session:
            async with session.begin():
                owner = await session.scalar(
                    select(UserRow).where(UserRow.telegram_id == telegram_user_id)
                )
                if owner is None:
                    return
                await session.execute(
                    update(TaskRow)
                    .where(
                        TaskRow.id.in_(task_ids),
                        TaskRow.owner_user_id == owner.id,
                        TaskRow.status.notin_(("done", "cancelled")),
                    )
                    .values(status="today")
                )

    async def _get_or_create_user(
        self,
        session: AsyncSession,
        telegram_user_id: int,
        telegram_chat_id: int,
    ) -> UserRow:
        existing = await session.scalar(
            select(UserRow).where(UserRow.telegram_id == telegram_user_id)
        )
        if existing is not None:
            if existing.telegram_chat_id != telegram_chat_id:
                existing.telegram_chat_id = telegram_chat_id
            return existing
        row = UserRow(telegram_id=telegram_user_id, telegram_chat_id=telegram_chat_id)
        session.add(row)
        await session.flush()
        return row

    async def get_address_form(self, telegram_user_id: int) -> str | None:
        async with self._sessions()() as session:
            owner = await session.scalar(
                select(UserRow).where(UserRow.telegram_id == telegram_user_id)
            )
            if owner is None:
                return None
            return owner.address_form

    async def set_address_form(
        self,
        telegram_user_id: int,
        form: str,
        *,
        telegram_chat_id: int,
    ) -> None:
        async with self._sessions()() as session:
            async with session.begin():
                owner = await self._get_or_create_user(
                    session, telegram_user_id, telegram_chat_id
                )
                owner.address_form = form

    async def create_user_project(
        self,
        telegram_user_id: int,
        name: str,
        *,
        telegram_chat_id: int,
    ) -> ResolvedProject:
        display = display_project_name(name)
        if not display:
            raise ValueError("invalid_name")
        async with self._sessions()() as session:
            async with session.begin():
                owner = await self._get_or_create_user(
                    session, telegram_user_id, telegram_chat_id
                )
                hit = await self._lookup_project_hit(session, owner.id, display)
                if hit is not None:
                    row, via = hit
                    if via == "alias":
                        raise ValueError(f"exists_alias:{row.name}")
                    raise ValueError(f"exists_canonical:{row.name}")
                row = ProjectRow(
                    owner_user_id=owner.id,
                    name=display,
                    name_normalized=normalize_project_name(display),
                    status="active",
                )
                session.add(row)
                await session.flush()
                return _resolved(row)

    async def _lookup_project(
        self,
        session: AsyncSession,
        owner_user_id: UUID,
        name: str,
        *,
        include_archived: bool = False,
    ) -> ProjectRow | None:
        hit = await self._lookup_project_hit(
            session, owner_user_id, name, include_archived=include_archived
        )
        return None if hit is None else hit[0]

    async def _lookup_project_hit(
        self,
        session: AsyncSession,
        owner_user_id: UUID,
        name: str,
        *,
        include_archived: bool = False,
    ) -> tuple[ProjectRow, str] | None:
        key = normalize_project_name(name)
        canonical = await session.scalar(
            select(ProjectRow).where(
                ProjectRow.owner_user_id == owner_user_id,
                ProjectRow.name_normalized == key,
            )
        )
        if canonical is not None:
            if canonical.status == "active" or include_archived:
                return canonical, "canonical"
            return None
        alias = await session.scalar(
            select(ProjectAliasRow).where(
                ProjectAliasRow.owner_user_id == owner_user_id,
                ProjectAliasRow.normalized_alias == key,
            )
        )
        if alias is None:
            return None
        project = await session.get(ProjectRow, alias.project_id)
        if project is None:
            return None
        if project.status != "active" and not include_archived:
            return None
        return project, "alias"

    async def resolve_user_project(
        self,
        telegram_user_id: int,
        name: str,
        *,
        include_archived: bool = False,
    ) -> ResolvedProject | None:
        async with self._sessions()() as session:
            owner = await session.scalar(
                select(UserRow).where(UserRow.telegram_id == telegram_user_id)
            )
            if owner is None:
                return None
            row = await self._lookup_project(
                session, owner.id, name, include_archived=include_archived
            )
            if row is None:
                return None
            return _resolved(row)

    async def suggest_similar_project(
        self, telegram_user_id: int, name: str
    ) -> ResolvedProject | None:
        async with self._sessions()() as session:
            owner = await session.scalar(
                select(UserRow).where(UserRow.telegram_id == telegram_user_id)
            )
            if owner is None:
                return None
            if await self._lookup_project(session, owner.id, name) is not None:
                return None
            rows = (
                await session.scalars(
                    select(ProjectRow).where(
                        ProjectRow.owner_user_id == owner.id,
                        ProjectRow.status == "active",
                    )
                )
            ).all()
            aliases = (
                await session.scalars(
                    select(ProjectAliasRow).where(ProjectAliasRow.owner_user_id == owner.id)
                )
            ).all()
            hits: list[ResolvedProject] = []
            seen: set[UUID] = set()
            for row in rows:
                if projects_look_similar(name, row.name) and row.id not in seen:
                    hits.append(_resolved(row))
                    seen.add(row.id)
            for alias in aliases:
                if not projects_look_similar(name, alias.alias):
                    continue
                if alias.project_id in seen:
                    continue
                project = await session.get(ProjectRow, alias.project_id)
                if project is None or project.status != "active":
                    continue
                hits.append(_resolved(project))
                seen.add(project.id)
            if len(hits) == 1:
                return hits[0]
            return None

    async def list_projects_for_user(
        self,
        telegram_user_id: int,
        *,
        status: str = "active",
    ) -> list[tuple[str, int]]:
        async with self._sessions()() as session:
            owner = await session.scalar(
                select(UserRow).where(UserRow.telegram_id == telegram_user_id)
            )
            if owner is None:
                return []
            open_owned = and_(
                TaskRow.status.in_(OPEN_TASK_STATUSES),
                _task_visible_to_owner(owner.id),
            )
            rows = (
                await session.execute(
                    select(ProjectRow.name, func.count(func.distinct(TaskRow.id)))
                    .outerjoin(
                        TaskRow,
                        (TaskRow.project_id == ProjectRow.id) & open_owned,
                    )
                    .where(
                        ProjectRow.owner_user_id == owner.id,
                        ProjectRow.status == status,
                    )
                    .group_by(ProjectRow.id, ProjectRow.name)
                    .order_by(ProjectRow.name)
                )
            ).all()
            return [(name, int(count)) for name, count in rows]
        async with self._sessions()() as session:
            owner = await session.scalar(
                select(UserRow).where(UserRow.telegram_id == telegram_user_id)
            )
            if owner is None:
                return []
            open_owned = and_(
                TaskRow.status.in_(OPEN_TASK_STATUSES),
                _task_visible_to_owner(owner.id),
            )
            rows = (
                await session.execute(
                    select(ProjectRow.name, func.count(func.distinct(TaskRow.id)))
                    .outerjoin(
                        TaskRow,
                        (TaskRow.project_id == ProjectRow.id) & open_owned,
                    )
                    .where(
                        ProjectRow.owner_user_id == owner.id,
                        ProjectRow.status == "active",
                    )
                    .group_by(ProjectRow.id, ProjectRow.name)
                    .order_by(ProjectRow.name)
                )
            ).all()
            return [(name, int(count)) for name, count in rows]

    async def list_open_tasks_for_project(
        self, telegram_user_id: int, project_id: UUID
    ) -> list[PlanCandidate]:
        return await self.list_user_tasks(
            telegram_user_id,
            include_archived_projects=True,
            project_id=project_id,
        )

    async def list_open_tasks_for_project_name(
        self, telegram_user_id: int, name: str
    ) -> tuple[str, list[PlanCandidate]] | None:
        resolved = await self.resolve_user_project(
            telegram_user_id, name, include_archived=True
        )
        if resolved is None:
            return None
        return resolved.name, await self.list_open_tasks_for_project(
            telegram_user_id, resolved.id
        )

    async def add_alias(
        self, telegram_user_id: int, project_name: str, alias: str
    ) -> ResolvedProject | None:
        async with self._sessions()() as session:
            async with session.begin():
                owner = await session.scalar(
                    select(UserRow).where(UserRow.telegram_id == telegram_user_id)
                )
                if owner is None:
                    return None
                project = await self._lookup_project(session, owner.id, project_name)
                if project is None:
                    return None
                await self._ensure_alias(session, owner.id, project.id, alias)
                return _resolved(project)

    async def rename_project(
        self, telegram_user_id: int, old_name: str, new_name: str
    ) -> ResolvedProject:
        async with self._sessions()() as session:
            async with session.begin():
                owner = await self._require_owner(session, telegram_user_id)
                project = await self._lookup_project(session, owner.id, old_name)
                if project is None:
                    raise ValueError("project_not_found")
                new_key = normalize_project_name(new_name)
                clash = await session.scalar(
                    select(ProjectRow).where(
                        ProjectRow.owner_user_id == owner.id,
                        ProjectRow.name_normalized == new_key,
                        ProjectRow.id != project.id,
                    )
                )
                if clash is not None:
                    raise ValueError("name_taken")
                old_display = project.name
                old_key = project.name_normalized
                project.name = display_project_name(new_name)
                project.name_normalized = new_key
                if old_key != new_key:
                    await self._ensure_alias(session, owner.id, project.id, old_display)
                await session.flush()
                return _resolved(project)

    async def merge_projects(
        self,
        telegram_user_id: int,
        keep_name: str,
        source_names: Sequence[str],
    ) -> tuple[ResolvedProject, int]:
        async with self._sessions()() as session:
            async with session.begin():
                owner = await self._require_owner(session, telegram_user_id)
                keep = await self._lookup_project(session, owner.id, keep_name)
                if keep is None:
                    raise ValueError("keep_not_found")
                moved = 0
                for source_name in source_names:
                    source = await self._lookup_project(session, owner.id, source_name)
                    if source is None:
                        raise ValueError("source_not_found")
                    if source.id == keep.id:
                        continue
                    moved += await self._merge_into(session, owner.id, keep, source)
                return _resolved(keep), moved

    async def count_open_tasks(self, telegram_user_id: int, name: str) -> int | None:
        resolved = await self.resolve_user_project(telegram_user_id, name)
        if resolved is None:
            return None
        listed = await self.list_open_tasks_for_project_name(telegram_user_id, name)
        if listed is None:
            return None
        return len(listed[1])

    async def _merge_into(
        self,
        session: AsyncSession,
        owner_user_id: UUID,
        keep: ProjectRow,
        source: ProjectRow,
    ) -> int:
        result = await session.execute(
            update(TaskRow)
            .where(TaskRow.project_id == source.id, TaskRow.owner_user_id == owner_user_id)
            .values(project_id=keep.id)
        )
        moved = int(result.rowcount or 0)
        aliases = (
            await session.scalars(
                select(ProjectAliasRow).where(ProjectAliasRow.project_id == source.id)
            )
        ).all()
        for alias in aliases:
            await self._ensure_alias(session, owner_user_id, keep.id, alias.alias)
            await session.delete(alias)
        await self._ensure_alias(session, owner_user_id, keep.id, source.name)
        await session.delete(source)
        await session.flush()
        return moved

    async def _ensure_alias(
        self,
        session: AsyncSession,
        owner_user_id: UUID,
        project_id: UUID,
        alias: str,
    ) -> None:
        key = normalize_project_name(alias)
        if not key:
            return
        project = await session.get(ProjectRow, project_id)
        if project is not None and project.name_normalized == key:
            return
        existing = await session.scalar(
            select(ProjectAliasRow).where(
                ProjectAliasRow.owner_user_id == owner_user_id,
                ProjectAliasRow.normalized_alias == key,
            )
        )
        if existing is not None:
            existing.project_id = project_id
            existing.alias = display_project_name(alias)
            return
        session.add(
            ProjectAliasRow(
                project_id=project_id,
                owner_user_id=owner_user_id,
                alias=display_project_name(alias),
                normalized_alias=key,
            )
        )

    async def count_all_tasks_on_project(
        self, telegram_user_id: int, project_id: UUID
    ) -> int:
        async with self._sessions()() as session:
            owner = await session.scalar(
                select(UserRow).where(UserRow.telegram_id == telegram_user_id)
            )
            if owner is None:
                return 0
            total = await session.scalar(
                select(func.count())
                .select_from(TaskRow)
                .where(
                    TaskRow.project_id == project_id,
                    TaskRow.owner_user_id == owner.id,
                )
            )
            return int(total or 0)

    async def count_project_history(
        self, telegram_user_id: int, project_id: UUID
    ) -> int:
        """Any task row on this project, including legacy NULL-owner tasks."""
        async with self._sessions()() as session:
            owner = await session.scalar(
                select(UserRow).where(UserRow.telegram_id == telegram_user_id)
            )
            if owner is None:
                return 0
            project = await session.get(ProjectRow, project_id)
            if project is None or project.owner_user_id != owner.id:
                return 0
            total = await session.scalar(
                select(func.count()).select_from(TaskRow).where(TaskRow.project_id == project_id)
            )
            return int(total or 0)

    async def set_user_project_status(
        self,
        telegram_user_id: int,
        name: str,
        status: str,
    ) -> ResolvedProject:
        if status not in {"active", "archived"}:
            raise ValueError("invalid_status")
        async with self._sessions()() as session:
            async with session.begin():
                owner = await self._require_owner(session, telegram_user_id)
                project = await self._lookup_project(
                    session, owner.id, name, include_archived=True
                )
                if project is None:
                    raise ValueError("project_not_found")
                project.status = status
                await session.flush()
                return _resolved(project)

    async def delete_empty_user_project(self, telegram_user_id: int, name: str) -> str:
        async with self._sessions()() as session:
            async with session.begin():
                owner = await self._require_owner(session, telegram_user_id)
                project = await self._lookup_project(
                    session, owner.id, name, include_archived=True
                )
                if project is None:
                    raise ValueError("project_not_found")
                history = await session.scalar(
                    select(func.count())
                    .select_from(TaskRow)
                    .where(TaskRow.project_id == project.id)
                )
                if int(history or 0) > 0:
                    return "has_history"
                await session.execute(
                    delete(ProjectAliasRow).where(ProjectAliasRow.project_id == project.id)
                )
                await session.delete(project)
                await session.flush()
                return "deleted"

    async def _require_owner(self, session: AsyncSession, telegram_user_id: int) -> UserRow:
        owner = await session.scalar(
            select(UserRow).where(UserRow.telegram_id == telegram_user_id)
        )
        if owner is None:
            raise ValueError("user_not_found")
        return owner

    async def list_person_aliases_for_user(
        self, telegram_user_id: int
    ) -> list[PersonAliasHit]:
        async with self._sessions()() as session:
            owner = await session.scalar(
                select(UserRow).where(UserRow.telegram_id == telegram_user_id)
            )
            if owner is None:
                return []
            rows = (
                await session.execute(
                    select(PersonAliasRow, PersonRow.name)
                    .join(PersonRow, PersonRow.id == PersonAliasRow.person_id)
                    .where(PersonAliasRow.owner_user_id == owner.id)
                )
            ).all()
            return [
                PersonAliasHit(alias=alias.alias, person_id=alias.person_id, person_name=name)
                for alias, name in rows
            ]

    async def confirm_person_equivalence(
        self,
        telegram_user_id: int,
        canonical_name: str,
        alias_name: str,
    ) -> PersonEquivalenceOutcome:
        async with self._sessions()() as session:
            async with session.begin():
                owner = await session.scalar(
                    select(UserRow).where(UserRow.telegram_id == telegram_user_id)
                )
                if owner is None:
                    return PersonEquivalenceOutcome(
                        status="not_found",
                        canonical_name=canonical_name,
                        alias=alias_name,
                    )
                keep = await self._find_person_row(session, owner.id, canonical_name)
                if keep is None:
                    return PersonEquivalenceOutcome(
                        status="not_found",
                        canonical_name=canonical_name,
                        alias=alias_name,
                    )
                conflict = await self._alias_conflict(session, owner.id, alias_name, keep.id)
                if conflict is not None:
                    return PersonEquivalenceOutcome(
                        status="conflict",
                        canonical_name=keep.name,
                        alias=alias_name,
                        conflict_with=conflict,
                    )
                source = await self._find_person_row(session, owner.id, alias_name)
                merged = False
                if source is not None and source.id != keep.id:
                    await self._merge_person_into(session, owner.id, keep, source)
                    merged = True
                await self._ensure_person_alias(session, owner.id, keep.id, alias_name)
                if source is not None and source.id != keep.id:
                    await self._ensure_person_alias(session, owner.id, keep.id, source.name)
                await session.flush()
                return PersonEquivalenceOutcome(
                    status="ok",
                    canonical_name=keep.name,
                    alias=alias_name,
                    merged=merged,
                )

    async def _find_person_row(
        self, session: AsyncSession, owner_user_id: UUID, name: str
    ) -> PersonRow | None:
        key = normalize_entity_name(name)
        exact = await session.scalar(select(PersonRow).where(PersonRow.name_normalized == key))
        if exact is not None:
            return exact
        people = (await session.scalars(select(PersonRow))).all()
        hits = [row for row in people if inflection_variant(name, row.name)]
        if len(hits) == 1:
            return hits[0]
        owned_ids = await self._owned_person_ids(session, owner_user_id)
        owned_hits = [row for row in hits if row.id in owned_ids]
        if len(owned_hits) == 1:
            return owned_hits[0]
        return None

    async def _owned_person_ids(self, session: AsyncSession, owner_user_id: UUID) -> set[UUID]:
        via_people = select(TaskPersonRow.person_id).join(TaskRow).where(
            TaskRow.owner_user_id == owner_user_id
        )
        via_waiting = select(TaskRow.waiting_for_person_id).where(
            TaskRow.owner_user_id == owner_user_id,
            TaskRow.waiting_for_person_id.isnot(None),
        )
        ids: set[UUID] = set()
        for (person_id,) in (await session.execute(via_people)).all():
            ids.add(person_id)
        for (person_id,) in (await session.execute(via_waiting)).all():
            if person_id is not None:
                ids.add(person_id)
        return ids

    async def _alias_conflict(
        self,
        session: AsyncSession,
        owner_user_id: UUID,
        alias: str,
        keep_id: UUID,
    ) -> str | None:
        key = normalize_entity_name(alias)
        rows = (
            await session.scalars(
                select(PersonAliasRow).where(PersonAliasRow.owner_user_id == owner_user_id)
            )
        ).all()
        for row in rows:
            if row.person_id == keep_id:
                continue
            if row.normalized_alias == key or inflection_variant(alias, row.alias):
                person = await session.get(PersonRow, row.person_id)
                return person.name if person is not None else row.alias
        return None

    async def _merge_person_into(
        self,
        session: AsyncSession,
        owner_user_id: UUID,
        keep: PersonRow,
        source: PersonRow,
    ) -> None:
        links = (
            await session.scalars(
                select(TaskPersonRow)
                .join(TaskRow, TaskRow.id == TaskPersonRow.task_id)
                .where(
                    TaskPersonRow.person_id == source.id,
                    TaskRow.owner_user_id == owner_user_id,
                )
            )
        ).all()
        keep_task_ids = set(
            (
                await session.scalars(
                    select(TaskPersonRow.task_id).where(TaskPersonRow.person_id == keep.id)
                )
            ).all()
        )
        for link in links:
            task_id = link.task_id
            await session.delete(link)
            if task_id not in keep_task_ids:
                session.add(TaskPersonRow(task_id=task_id, person_id=keep.id))
                keep_task_ids.add(task_id)
        await session.flush()
        await session.execute(
            update(TaskRow)
            .where(
                TaskRow.owner_user_id == owner_user_id,
                TaskRow.waiting_for_person_id == source.id,
            )
            .values(waiting_for_person_id=keep.id)
        )
        aliases = (
            await session.scalars(
                select(PersonAliasRow).where(
                    PersonAliasRow.owner_user_id == owner_user_id,
                    PersonAliasRow.person_id == source.id,
                )
            )
        ).all()
        for alias in aliases:
            await self._ensure_person_alias(session, owner_user_id, keep.id, alias.alias)
            await session.delete(alias)
        await session.flush()
        leftover_links = await session.scalar(
            select(func.count()).select_from(TaskPersonRow).where(TaskPersonRow.person_id == source.id)
        )
        leftover_wait = await session.scalar(
            select(func.count()).select_from(TaskRow).where(TaskRow.waiting_for_person_id == source.id)
        )
        leftover_aliases = await session.scalar(
            select(func.count())
            .select_from(PersonAliasRow)
            .where(PersonAliasRow.person_id == source.id)
        )
        if not leftover_links and not leftover_wait and not leftover_aliases:
            await session.delete(source)

    async def _ensure_person_alias(
        self,
        session: AsyncSession,
        owner_user_id: UUID,
        person_id: UUID,
        alias: str,
    ) -> None:
        key = normalize_entity_name(alias)
        if not key:
            return
        person = await session.get(PersonRow, person_id)
        if person is not None and (
            person.name_normalized == key or inflection_variant(alias, person.name)
        ):
            return
        existing = (
            await session.scalars(
                select(PersonAliasRow).where(PersonAliasRow.owner_user_id == owner_user_id)
            )
        ).all()
        for row in existing:
            if row.normalized_alias == key or inflection_variant(alias, row.alias):
                row.person_id = person_id
                row.alias = " ".join(alias.split())
                row.normalized_alias = key
                return
        session.add(
            PersonAliasRow(
                person_id=person_id,
                owner_user_id=owner_user_id,
                alias=" ".join(alias.split()),
                normalized_alias=key,
            )
        )

    async def _get_or_create_person(
        self,
        session: AsyncSession,
        name: str,
        owner_user_id: UUID | None = None,
    ) -> PersonRow:
        return await self._resolve_or_create_person(session, name, owner_user_id=owner_user_id)

    async def _resolve_or_create_person(
        self,
        session: AsyncSession,
        name: str,
        owner_user_id: UUID | None = None,
    ) -> PersonRow:
        if owner_user_id is not None:
            aliased = await self._person_via_alias(session, owner_user_id, name)
            if aliased is not None:
                return aliased
        key = normalize_entity_name(name)
        existing = await session.scalar(
            select(PersonRow).where(PersonRow.name_normalized == key)
        )
        if existing is not None:
            return existing
        people = (await session.execute(select(PersonRow))).scalars().all()
        for person in people:
            if _person_names_match(key, person.name_normalized):
                return person
        display = " ".join(name.split())
        row = PersonRow(name=display, name_normalized=key)
        session.add(row)
        await session.flush()
        return row

    async def _person_via_alias(
        self, session: AsyncSession, owner_user_id: UUID, name: str
    ) -> PersonRow | None:
        key = normalize_entity_name(name)
        rows = (
            await session.scalars(
                select(PersonAliasRow).where(PersonAliasRow.owner_user_id == owner_user_id)
            )
        ).all()
        for row in rows:
            if row.normalized_alias == key or inflection_variant(name, row.alias):
                person = await session.get(PersonRow, row.person_id)
                if person is not None:
                    return person
        return None

    async def _link_people(
        self,
        session: AsyncSession,
        task_id: UUID,
        names: tuple[str, ...],
        owner_user_id: UUID | None = None,
    ) -> None:
        seen: set[str] = set()
        for raw in names:
            key = normalize_entity_name(raw)
            if not key or key in seen:
                continue
            seen.add(key)
            person = await self._get_or_create_person(session, raw, owner_user_id=owner_user_id)
            session.add(TaskPersonRow(task_id=task_id, person_id=person.id))
        await session.flush()


def _person_names_match(query: str, stored: str) -> bool:
    left = query.split()[0] if query.split() else ""
    right = stored.split()[0] if stored.split() else ""
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) >= 4 and (left.startswith(right[:4]) or right.startswith(left[:4])):
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.86
