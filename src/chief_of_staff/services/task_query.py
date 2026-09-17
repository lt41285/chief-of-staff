"""Read-only task lists. Never creates or updates tasks."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from loguru import logger

from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.models.task_command import QUERY_INTENT_KINDS, TaskIntent, TaskIntentKind
from chief_of_staff.services.candidate_search import SearchBundle, search_person_tasks
from chief_of_staff.services.fact_reply import format_search_facts
from chief_of_staff.services.person_match import PersonHit, people_names_match, resolve_person
from chief_of_staff.services.person_resolution import resolve_person_reference
from chief_of_staff.services.person_relevance import explicit_name_in_text, filter_related
from chief_of_staff.services.task_facts import ListedTaskFact, facts_from_tasks
from chief_of_staff.services.task_command_intent import (
    TaskIntentParser,
    looks_like_task_query,
    parse_task_intent_deterministic,
)
from chief_of_staff.services.task_query_format import (
    format_all_tasks_grouped,
    format_person_mention_list,
    format_person_picker,
    format_person_task_list,
    format_project_task_list,
    format_unknown_person,
    format_unknown_project,
)


class QueryKind(StrEnum):
    TASKS = "tasks"
    INFO = "info"


@dataclass(frozen=True)
class QueryResult:
    kind: QueryKind
    text: str
    person_name: str | None = None
    project_name: str | None = None
    task_ids: tuple[UUID, ...] = ()
    titles: tuple[str, ...] = ()
    discuss: bool = False
    status_filter: str | None = None
    person_query: str | None = None
    replace_person: bool = False
    listed_facts: tuple[ListedTaskFact, ...] = ()
    total_estimated_minutes: int | None = None
    missing_estimates: int = 0
    period_label: str | None = None
    overdue_ids: tuple[UUID, ...] = ()
    last_available_minutes: int | None = None
    is_person_ambiguity: bool = False
    ambiguity_candidates: tuple[str, ...] = ()


class TaskQueryService:
    def __init__(
        self,
        repository: SqlAlchemyTaskRepository,
        parser: TaskIntentParser | None = None,
    ) -> None:
        self._repository = repository
        self._parser = parser

    async def confirm_person_equivalence(
        self, user_id: int, canonical: str, alias: str
    ):
        return await self._repository.confirm_person_equivalence(user_id, canonical, alias)

    async def classify(self, text: str) -> TaskIntent | None:
        parsed = parse_task_intent_deterministic(text)
        if parsed is not None and parsed.kind in QUERY_INTENT_KINDS:
            return parsed
        if not looks_like_task_query(text):
            return None
        if self._parser is not None:
            try:
                incoming = await self._parser.parse(text)
                if incoming.kind in QUERY_INTENT_KINDS:
                    return incoming
            except Exception:
                logger.exception("Task query intent parse failed")
        return TaskIntent(kind=TaskIntentKind.LIST_ALL_TASKS)

    async def handle_intent(self, user_id: int, intent: TaskIntent) -> QueryResult:
        if intent.kind == TaskIntentKind.PEOPLE_TASKS_QUERY:
            if intent.broader_search or intent.exclude_person:
                return await self.search_people(user_id, intent)
            return await self._list_person(user_id, intent)
        if intent.kind == TaskIntentKind.LIST_PROJECT_TASKS:
            return await self._list_project(
                user_id, intent.project_query or "", intent.status_filter
            )
        return await self._list_all(user_id, intent.status_filter)

    async def search_people(self, user_id: int, intent: TaskIntent) -> QueryResult:
        query = (intent.person_query or "").strip().strip(" —–-")
        if not query:
            return QueryResult(kind=QueryKind.INFO, text=format_unknown_person(""))
        original_query = query
        people = await self._repository.list_people_for_user(user_id)
        aliases = await self._repository.list_person_aliases_for_user(user_id)
        resolved = resolve_person_reference(query, people, aliases)
        if resolved.status == "ambiguous":
            names = tuple(person.name for person in resolved.candidates)
            return QueryResult(
                kind=QueryKind.INFO,
                text=_format_person_ambiguity(query, names),
                person_query=query,
                is_person_ambiguity=True,
                ambiguity_candidates=names,
                status_filter=intent.status_filter,
            )
        search_name = query
        if resolved.status == "resolved" and resolved.person is not None:
            people = [resolved.person]
            search_name = resolved.person.name
        tasks = await self._tasks_for_user(user_id, intent.status_filter)
        exclude = tuple(
            name for name in (intent.exclude_person,) if name and name.strip()
        )
        bundle = search_person_tasks(
            search_name,
            people,
            tasks,
            exclude_names=exclude,
            broader=bool(intent.broader_search),
        )
        if resolved.status == "resolved" and resolved.person is not None:
            bundle = _expand_resolved_bundle(
                bundle, tasks, original_query, aliases, resolved.person
            )
        if intent.project_query:
            bundle = SearchBundle(
                query=bundle.query,
                people=bundle.people,
                tasks=tuple(
                    task
                    for task in bundle.tasks
                    if intent.project_query.casefold() in task.project.casefold()
                ),
                exact_person=bundle.exact_person,
                label=bundle.label,
            )
        if intent.deadline_on:
            bundle = SearchBundle(
                query=bundle.query,
                people=bundle.people,
                tasks=tuple(
                    task
                    for task in bundle.tasks
                    if task.deadline.isoformat() == intent.deadline_on
                ),
                exact_person=bundle.exact_person,
                label=bundle.label,
            )
        if intent.status_filter == "waiting":
            bundle = SearchBundle(
                query=bundle.query,
                people=bundle.people,
                tasks=tuple(task for task in bundle.tasks if task.status == "waiting"),
                exact_person=bundle.exact_person,
                label=bundle.label,
            )
        if intent.status_filter == "done":
            bundle = SearchBundle(
                query=bundle.query,
                people=bundle.people,
                tasks=tuple(task for task in bundle.tasks if task.status == "done"),
                exact_person=bundle.exact_person,
                label=bundle.label,
            )
        if intent.task_query == "discuss":
            bundle = SearchBundle(
                query=bundle.query,
                people=bundle.people,
                tasks=tuple(
                    task
                    for task in bundle.tasks
                    if task.people or task.status == "waiting"
                ),
                exact_person=bundle.exact_person,
                label=bundle.label,
                status_filter=intent.status_filter,
            )
        bundle = SearchBundle(
            query=bundle.query,
            people=bundle.people,
            tasks=bundle.tasks,
            exact_person=bundle.exact_person,
            label=bundle.label,
            status_filter=intent.status_filter,
        )
        text = format_search_facts(bundle)
        listed = list(bundle.tasks)
        return QueryResult(
            kind=QueryKind.TASKS if bundle.tasks or bundle.people else QueryKind.INFO,
            text=text,
            person_name=bundle.exact_person or bundle.label,
            person_query=bundle.query,
            replace_person=True,
            task_ids=tuple(task.id for task in listed),
            titles=tuple(task.title for task in listed),
            discuss=intent.task_query == "discuss",
            status_filter=intent.status_filter,
            listed_facts=facts_from_tasks(listed),
        )

    async def _list_person(self, user_id: int, intent: TaskIntent) -> QueryResult:
        query = (intent.person_query or "").strip().strip(" —–-")
        if not query:
            return QueryResult(kind=QueryKind.INFO, text=format_unknown_person(""))
        people = await self._repository.list_people_for_user(user_id)
        aliases = await self._repository.list_person_aliases_for_user(user_id)
        resolved = resolve_person_reference(query, people, aliases)
        if resolved.status == "ambiguous":
            names = tuple(person.name for person in resolved.candidates)
            return QueryResult(
                kind=QueryKind.INFO,
                text=_format_person_ambiguity(query, names),
                person_query=query,
                is_person_ambiguity=True,
                ambiguity_candidates=names,
                status_filter=intent.status_filter,
            )
        if resolved.status == "resolved" and resolved.person is not None:
            matched_selected = resolved.person
            matched_candidates: tuple[PersonHit, ...] = ()
        else:
            matched = resolve_person(query, people)
            matched_selected = matched.selected
            matched_candidates = matched.candidates
        if matched_selected is None and matched_candidates:
            names = tuple(person.name for person in matched_candidates)
            return QueryResult(
                kind=QueryKind.INFO,
                text=format_person_picker(query, matched_candidates),
                person_query=query,
                is_person_ambiguity=True,
                ambiguity_candidates=names,
                status_filter=intent.status_filter,
            )
        tasks = await self._tasks_for_user(user_id, intent.status_filter)
        display_name = matched_selected.name if matched_selected is not None else query
        related: list[PlanCandidate] = []
        linked = False
        if matched_selected is not None:
            linked = True
            related = _related_tasks(
                tasks,
                matched_selected,
                waiting_only=intent.status_filter == "waiting",
                discuss=intent.task_query == "discuss",
            )
            for task in tasks:
                if task.id in {item.id for item in related}:
                    continue
                if explicit_name_in_text(task, query):
                    related.append(task)
        if not related:
            related = filter_related(tasks, display_name)
            if intent.status_filter == "waiting":
                related = [
                    task
                    for task in related
                    if task.status == "waiting"
                    and task.waiting_for
                    and people_names_match(task.waiting_for, display_name)
                ]
            elif intent.task_query == "discuss":
                related = [
                    task
                    for task in related
                    if display_name.casefold() in " ".join(task.people).casefold()
                    or task.status == "waiting"
                ]
            linked = False if related else linked
        if intent.project_query:
            related = [
                task
                for task in related
                if intent.project_query.casefold() in task.project.casefold()
            ]
        if intent.deadline_on:
            related = [task for task in related if task.deadline.isoformat() == intent.deadline_on]
        related.sort(key=lambda task: (task.project.casefold(), task.title.casefold()))
        if not related and matched_selected is None:
            project = await self._repository.resolve_user_project(user_id, query)
            if project is not None:
                return await self._list_project(user_id, project.name, None)
            return QueryResult(kind=QueryKind.INFO, text=format_unknown_person(query))
        if linked or matched_selected is not None:
            text = format_person_task_list(
                display_name,
                related,
                empty_query=query,
                status_filter=intent.status_filter,
            )
        else:
            text = format_person_mention_list(
                display_name, related, status_filter=intent.status_filter
            )
        return _query_with_tasks(
            text,
            related,
            person_name=display_name,
            person_query=query,
            discuss=intent.task_query == "discuss",
            status_filter=intent.status_filter,
        )

    async def _list_project(
        self,
        user_id: int,
        project_query: str,
        status_filter: str | None,
    ) -> QueryResult:
        name = project_query.strip()
        if not name:
            return await self._list_all(user_id, status_filter)
        resolved = await self._repository.resolve_user_project(
            user_id, name, include_archived=True
        )
        if resolved is None:
            return QueryResult(kind=QueryKind.INFO, text=format_unknown_project(name))
        if resolved.status == "archived":
            tasks = await self._repository.list_open_tasks_for_project(user_id, resolved.id)
            header = f"📦 Проєкт «{resolved.name}» в архіві."
            body = format_project_task_list(
                resolved.name, tasks, status_filter=status_filter
            )
            return _query_with_tasks(
                f"{header}\n\n{body}",
                tasks,
                project_name=resolved.name,
                status_filter=status_filter,
            )
        tasks = await self._tasks_for_user(user_id, status_filter)
        in_project = [task for task in tasks if task.project_id == resolved.id]
        return _query_with_tasks(
            format_project_task_list(
                resolved.name, in_project, status_filter=status_filter
            ),
            in_project,
            project_name=resolved.name,
            status_filter=status_filter,
        )

    async def _list_all(self, user_id: int, status_filter: str | None) -> QueryResult:
        tasks = await self._tasks_for_user(user_id, status_filter)
        groups: dict[str, list[PlanCandidate]] = {}
        for task in tasks:
            groups.setdefault(task.project, []).append(task)
        ordered = tuple(sorted(groups.items(), key=lambda item: item[0].casefold()))
        return _query_with_tasks(
            format_all_tasks_grouped(
                ordered, total=len(tasks), status_filter=status_filter
            ),
            tasks,
            status_filter=status_filter,
        )

    async def list_open_tasks(self, user_id: int) -> list[PlanCandidate]:
        return await self._tasks_for_user(user_id, None)

    async def list_planning_candidates(self, user_id: int) -> list[PlanCandidate]:
        listed = getattr(self._repository, "list_open_tasks_for_telegram_user", None)
        if listed is not None:
            return await listed(user_id)
        return await self.list_open_tasks(user_id)

    async def describe_listed_status(
        self,
        user_id: int,
        task_ids: tuple[UUID, ...] | list[UUID],
        *,
        person_name: str | None = None,
    ) -> QueryResult:
        tasks = await self.tasks_by_ids(user_id, task_ids, include_closed=True)
        if not tasks:
            return QueryResult(
                kind=QueryKind.INFO,
                text="Не бачу тих задач у базі. Назви їх ще раз.",
                person_name=person_name,
            )
        done = [task for task in tasks if task.status == "done"]
        cancelled = [task for task in tasks if task.status == "cancelled"]
        opened = [task for task in tasks if task.status not in {"done", "cancelled"}]
        lines = [
            "Перевірив статус у базі — орієнтуюсь на записи, а не на формулювання:"
        ]
        if done:
            label = "уже виконана" if len(done) == 1 else "уже виконані"
            lines.append(f"{len(done)} {label}:")
            lines.extend(f"— {task.title}" for task in done[:12])
        if cancelled:
            lines.append(f"{len(cancelled)} скасовані:")
            lines.extend(f"— {task.title}" for task in cancelled[:8])
        if opened:
            label = "досі відкрита" if len(opened) == 1 else "досі відкриті"
            lines.append(f"{len(opened)} {label}:")
            lines.extend(f"— {task.title}" for task in opened[:12])
        elif done:
            lines.append("Відкритих серед них немає.")
        return QueryResult(
            kind=QueryKind.INFO,
            text="\n".join(lines),
            person_name=person_name,
            task_ids=tuple(task.id for task in tasks),
            titles=tuple(task.title for task in tasks),
            listed_facts=facts_from_tasks(tasks),
            status_filter="done" if done and not opened else None,
        )

    async def tasks_by_ids(
        self,
        user_id: int,
        task_ids: tuple[UUID, ...] | list[UUID],
        *,
        include_closed: bool = False,
    ) -> list[PlanCandidate]:
        found: list[PlanCandidate] = []
        for task_id in task_ids:
            task = await self._repository.get_user_task(user_id, task_id)
            if task is None:
                continue
            if not include_closed and task.status in {"done", "cancelled"}:
                continue
            found.append(task)
        return found

    async def _tasks_for_user(
        self, user_id: int, status_filter: str | None
    ) -> list[PlanCandidate]:
        if status_filter == "done":
            return await self._repository.list_user_tasks(user_id, only_statuses=("done",))
        if status_filter == "waiting":
            return await self._repository.list_user_tasks(user_id, only_statuses=("waiting",))
        return await self._repository.list_user_tasks(user_id)


def _expand_resolved_bundle(
    bundle: SearchBundle,
    tasks: list[PlanCandidate],
    original_query: str,
    aliases: list,
    person: PersonHit,
) -> SearchBundle:
    known = {task.id for task in bundle.tasks}
    extra: list[PlanCandidate] = []
    alias_names = [item.alias for item in aliases if item.person_id == person.person_id]
    for task in tasks:
        if task.id in known:
            continue
        if explicit_name_in_text(task, original_query) or any(
            explicit_name_in_text(task, name) for name in alias_names
        ):
            extra.append(task)
            known.add(task.id)
    if not extra:
        return bundle
    merged = tuple(
        sorted(
            (*bundle.tasks, *extra),
            key=lambda task: (task.project.casefold(), task.title.casefold()),
        )
    )
    return SearchBundle(
        query=bundle.query,
        people=bundle.people,
        tasks=merged,
        exact_person=person.name,
        label=person.name,
    )


def _format_person_ambiguity(query: str, names: tuple[str, ...]) -> str:
    if len(names) == 2:
        return (
            f"Ти маєш на увазі «{names[0]}» чи «{names[1]}»? "
            "Якщо це одна людина — так і напиши."
        )
    options = " чи ".join(f"«{name}»" for name in names[:4])
    return f"Ти маєш на увазі {options}?"


def _related_tasks(
    tasks: list[PlanCandidate],
    person: PersonHit,
    *,
    waiting_only: bool,
    discuss: bool,
) -> list[PlanCandidate]:
    found: dict[object, PlanCandidate] = {}
    for task in tasks:
        in_people = any(people_names_match(name, person.name) for name in task.people)
        waiting_them = bool(
            task.waiting_for and people_names_match(task.waiting_for, person.name)
        )
        if waiting_only:
            keep = task.status == "waiting" and waiting_them
        elif discuss:
            keep = in_people or (task.status == "waiting" and waiting_them)
        else:
            keep = in_people or waiting_them
        if keep:
            found[task.id] = task
    return list(found.values())


def _query_with_tasks(
    text: str,
    tasks: list[PlanCandidate],
    *,
    person_name: str | None = None,
    person_query: str | None = None,
    project_name: str | None = None,
    discuss: bool = False,
    status_filter: str | None = None,
) -> QueryResult:
    return QueryResult(
        kind=QueryKind.TASKS,
        text=text,
        person_name=person_name,
        person_query=person_query,
        project_name=project_name,
        task_ids=tuple(task.id for task in tasks),
        titles=tuple(task.title for task in tasks),
        discuss=discuss,
        status_filter=status_filter,
        listed_facts=facts_from_tasks(tasks),
    )
