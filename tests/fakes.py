from uuid import UUID, uuid4

from chief_of_staff.infrastructure.database.repository import ResolvedProject
from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.models.task import TaskDraft
from chief_of_staff.services.names import display_project_name, normalize_project_name
from chief_of_staff.services.person_match import PersonHit


class FakeTaskRepository:
    def __init__(self, *, known_projects: tuple[str, ...] = ("Unity Center",)) -> None:
        self.saved: list[TaskDraft] = []
        self.last_owner: tuple[int, int] | None = None
        self.fail = False
        self.last_project_id: UUID | None = None
        self.open_tasks: dict[int, list[PlanCandidate]] = {}
        self.marked_today: list[tuple[int, tuple[UUID, ...]]] = []
        self.created_projects: list[str] = []
        self.projects: dict[str, ResolvedProject] = {}
        for name in known_projects:
            self._store_project(name)

    def _store_project(self, name: str) -> ResolvedProject:
        display = display_project_name(name)
        hit = ResolvedProject(id=uuid4(), name=display)
        self.projects[normalize_project_name(name)] = hit
        return hit

    async def create_user_project(
        self,
        telegram_user_id: int,
        name: str,
        *,
        telegram_chat_id: int,
    ) -> ResolvedProject:
        display = display_project_name(name)
        key = normalize_project_name(name)
        existing = self.projects.get(key)
        if existing is not None:
            raise ValueError(f"exists_canonical:{existing.name}")
        self.created_projects.append(display)
        return self._store_project(display)

    async def save_validated_task(
        self,
        draft: TaskDraft,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        project_id: UUID | None = None,
    ) -> UUID:
        if self.fail:
            raise RuntimeError("database unavailable")
        if draft.project and project_id is None:
            found = await self.resolve_user_project(telegram_user_id, draft.project)
            if found is None:
                raise ValueError("project_not_found")
        self.saved.append(draft)
        self.last_owner = (telegram_user_id, telegram_chat_id)
        self.last_project_id = project_id
        return uuid4()

    async def resolve_user_project(
        self, telegram_user_id: int, name: str, *, include_archived: bool = False
    ) -> ResolvedProject | None:
        return self.projects.get(normalize_project_name(name))

    async def suggest_similar_project(self, telegram_user_id: int, name: str) -> None:
        return None

    async def list_projects_for_user(
        self, telegram_user_id: int, *, status: str = "active"
    ) -> list[tuple[str, int]]:
        del telegram_user_id, status
        return [(hit.name, 0) for hit in self.projects.values()]

    async def list_user_tasks(
        self,
        telegram_user_id: int,
        *,
        exclude_statuses: tuple[str, ...] = ("done", "cancelled"),
        only_statuses: tuple[str, ...] | None = None,
    ) -> list[PlanCandidate]:
        tasks = list(self.open_tasks.get(telegram_user_id, []))
        if only_statuses is not None:
            return [task for task in tasks if task.status in only_statuses]
        return [task for task in tasks if task.status not in exclude_statuses]

    async def list_open_tasks_for_telegram_user(
        self, telegram_user_id: int
    ) -> list[PlanCandidate]:
        return await self.list_user_tasks(
            telegram_user_id, exclude_statuses=("done", "cancelled", "waiting")
        )

    async def list_people_for_user(self, telegram_user_id: int) -> list[PersonHit]:
        unique: dict[str, PersonHit] = {}
        for task in self.open_tasks.get(telegram_user_id, []):
            for name in task.people:
                key = name.casefold()
                unique.setdefault(key, PersonHit(name=name))
            if task.waiting_for:
                key = task.waiting_for.casefold()
                unique.setdefault(key, PersonHit(name=task.waiting_for))
        return list(unique.values())

    async def mark_tasks_today(
        self, telegram_user_id: int, task_ids: tuple[UUID, ...]
    ) -> None:
        self.marked_today.append((telegram_user_id, task_ids))
        for task in self.open_tasks.get(telegram_user_id, []):
            if task.id in task_ids:
                object.__setattr__(task, "status", "today")

    async def get_address_form(self, telegram_user_id: int) -> str | None:
        return getattr(self, "address_forms", {}).get(telegram_user_id)

    async def set_address_form(
        self,
        telegram_user_id: int,
        form: str,
        *,
        telegram_chat_id: int,
    ) -> None:
        forms = getattr(self, "address_forms", None)
        if forms is None:
            self.address_forms = {}
        self.address_forms[telegram_user_id] = form
