"""Python-owned tools: retrieval, date filters, and estimate math."""

from datetime import date
from uuid import UUID

from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.models.task_command import QUERY_INTENT_KINDS, TaskIntentKind
from chief_of_staff.models.utterance_intent import QueryRelation, RouterKind, UtteranceInterpretation
from chief_of_staff.services.conversation_context import ConversationSnapshot
from chief_of_staff.services.date_windows import resolve_period
from chief_of_staff.services.intent_router import interpretation_to_intent
from chief_of_staff.services.reminder_messages import format_uk_date
from chief_of_staff.services.task_facts import facts_from_tasks
from chief_of_staff.services.task_math import (
    EstimateSummary,
    filter_due_between,
    filter_overdue,
    fit_tasks_into_minutes,
    sum_estimated_minutes,
)
from chief_of_staff.services.task_query import QueryKind, QueryResult, TaskQueryService
from chief_of_staff.services.task_query_format import format_all_tasks_grouped, format_person_task_list


def needs_grounded_tools(interp: UtteranceInterpretation) -> bool:
    return interp.kind in {
        RouterKind.PEOPLE_TASKS_QUERY,
        RouterKind.LIST_PROJECT_TASKS,
        RouterKind.LIST_ALL_TASKS,
        RouterKind.INSPECT_LISTED,
        RouterKind.SUM_ESTIMATES,
        RouterKind.FIT_MINUTES,
        RouterKind.LIST_PERIOD,
        RouterKind.REFINE_PREVIOUS,
        RouterKind.RETRY_PREVIOUS,
        RouterKind.CORRECT_ENTITY,
    }


async def execute_grounded_tools(
    queries: TaskQueryService,
    user_id: int,
    interp: UtteranceInterpretation,
    snapshot: ConversationSnapshot | None,
    *,
    today: date,
) -> QueryResult:
    if _is_period_query(interp, snapshot):
        period_key = interp.period or (snapshot.last_period if snapshot else None)
        return await _period_query(
            queries,
            user_id,
            period_key,
            today,
            exclude_overdue=bool(interp.exclude_overdue)
            or interp.kind == RouterKind.REFINE_PREVIOUS,
            include_overdue=interp.include_overdue,
        )

    if interp.kind == RouterKind.SUM_ESTIMATES:
        tasks = await _listed_tasks(queries, user_id, interp, snapshot)
        return _sum_result(tasks, snapshot)

    if interp.kind == RouterKind.INSPECT_LISTED:
        tasks = await _listed_tasks(queries, user_id, interp, snapshot)
        return _inspect_result(tasks, snapshot)

    if interp.kind == RouterKind.FIT_MINUTES:
        scoped_person = interp.person_query
        if scoped_person:
            tasks = await _search_tasks(queries, user_id, interp)
        elif interp.query_relation in {QueryRelation.CONTINUE_QUERY, QueryRelation.REFINE_QUERY}:
            tasks = await _listed_tasks(queries, user_id, interp, snapshot)
            if not tasks:
                tasks = await _planning_candidates(queries, user_id)
        else:
            tasks = await _planning_candidates(queries, user_id)
        tasks = _apply_task_filters(tasks, interp, today)
        return _fit_result(
            tasks,
            interp.available_minutes or (snapshot.last_available_minutes if snapshot else None),
            person_name=scoped_person,
        )

    if interp.kind == RouterKind.REFINE_PREVIOUS and (
        interp.use_listed_ids or interp.exclude_indexes
    ):
        tasks = await _listed_tasks(queries, user_id, interp, snapshot)
        return _inspect_result(tasks, snapshot)

    if interp.kind == RouterKind.LIST_ALL_TASKS:
        mapped = interpretation_to_intent(interp)
        if mapped is None:
            tasks = await queries.list_open_tasks(user_id)
            tasks = _apply_task_filters(tasks, interp, today)
            return _global_list_result(tasks, interp)
        result = await queries.handle_intent(user_id, mapped)
        include_closed = mapped.status_filter == "done" or result.status_filter == "done"
        tasks = await queries.tasks_by_ids(
            user_id, result.task_ids, include_closed=include_closed
        )
        tasks = _apply_task_filters(tasks, interp, today)
        if interp.include_overdue or interp.clear_person_filter:
            return _global_list_result(tasks, interp)
        return _with_facts(
            QueryResult(
                kind=result.kind,
                text=result.text,
                person_name=None,
                person_query=None,
                project_name=result.project_name,
                task_ids=result.task_ids,
                titles=result.titles,
                listed_facts=result.listed_facts,
                status_filter=result.status_filter,
            ),
            tasks,
        )

    mapped = interpretation_to_intent(interp)
    if mapped is None or mapped.kind not in QUERY_INTENT_KINDS:
        tasks = await _listed_tasks(queries, user_id, interp, snapshot)
        return _inspect_result(tasks, snapshot)

    if mapped.kind == TaskIntentKind.PEOPLE_TASKS_QUERY:
        result = await queries.search_people(user_id, mapped)
    else:
        result = await queries.handle_intent(user_id, mapped)
    if result.is_person_ambiguity:
        return result
    include_closed = (mapped.status_filter == "done") or (result.status_filter == "done")
    tasks = await queries.tasks_by_ids(
        user_id, result.task_ids, include_closed=include_closed
    )
    tasks = _drop_indexes(tasks, interp.exclude_indexes)
    tasks = _apply_task_filters(tasks, interp, today)
    if interp.include_overdue or interp.period:
        result = _with_facts(result, tasks)
        if interp.person_query or result.person_name:
            label = result.person_name or interp.person_query or "задачі"
            result = QueryResult(
                kind=QueryKind.TASKS if tasks else QueryKind.INFO,
                text=format_person_task_list(
                    label,
                    tasks,
                    empty_query=interp.person_query or label,
                    status_filter=result.status_filter,
                ),
                person_name=result.person_name,
                person_query=result.person_query or interp.person_query,
                project_name=result.project_name,
                task_ids=tuple(task.id for task in tasks),
                titles=tuple(task.title for task in tasks),
                listed_facts=facts_from_tasks(tasks),
                status_filter=result.status_filter,
                discuss=result.discuss,
                overdue_ids=tuple(task.id for task in filter_overdue(tasks, today)),
            )
    if interp.available_minutes:
        return _fit_result(
            tasks,
            interp.available_minutes,
            person_name=result.person_name or interp.person_query,
        )
    return _with_facts(result, tasks)


def _is_period_query(
    interp: UtteranceInterpretation, snapshot: ConversationSnapshot | None
) -> bool:
    return interp.kind == RouterKind.LIST_PERIOD


async def _search_tasks(
    queries: TaskQueryService, user_id: int, interp: UtteranceInterpretation
) -> list[PlanCandidate]:
    mapped = interpretation_to_intent(
        interp.model_copy(update={"kind": RouterKind.PEOPLE_TASKS_QUERY})
    )
    if mapped is None:
        return []
    if mapped.kind == TaskIntentKind.PEOPLE_TASKS_QUERY:
        result = await queries.search_people(user_id, mapped)
    else:
        result = await queries.handle_intent(user_id, mapped)
    include_closed = mapped.status_filter == "done"
    return await queries.tasks_by_ids(
        user_id, result.task_ids, include_closed=include_closed
    )


async def _period_query(
    queries: TaskQueryService,
    user_id: int,
    period: str | None,
    today: date,
    *,
    exclude_overdue: bool,
    include_overdue: bool,
) -> QueryResult:
    window = resolve_period(period, today)
    if window is None:
        return QueryResult(
            kind=QueryKind.INFO,
            text="Не зрозумів період. Скажи «сьогодні», «цього тижня» або «наступного тижня».",
        )
    start, end = window
    open_tasks = await queries.list_open_tasks(user_id)
    due = filter_due_between(open_tasks, start, end)
    overdue = filter_overdue(open_tasks, today)
    if exclude_overdue:
        due = [task for task in due if task.deadline >= today]
    text = _format_period(period or "next_week", start, end, due, overdue)
    summary = sum_estimated_minutes(due)
    return QueryResult(
        kind=QueryKind.TASKS if due else QueryKind.INFO,
        text=text,
        task_ids=tuple(task.id for task in due),
        titles=tuple(task.title for task in due),
        listed_facts=facts_from_tasks(due),
        overdue_ids=tuple(task.id for task in overdue),
        period_label=period,
        total_estimated_minutes=summary.total_minutes,
        missing_estimates=summary.missing,
    )


async def _listed_tasks(
    queries: TaskQueryService,
    user_id: int,
    interp: UtteranceInterpretation,
    snapshot: ConversationSnapshot | None,
) -> list[PlanCandidate]:
    ids = _resolve_listed_ids(interp, snapshot)
    include_closed = False
    if interp.status_filter == "done" or (
        snapshot is not None and snapshot.status_filter == "done"
    ):
        include_closed = True
    return await queries.tasks_by_ids(user_id, ids, include_closed=include_closed)


def _resolve_listed_ids(
    interp: UtteranceInterpretation,
    snapshot: ConversationSnapshot | None,
) -> tuple[UUID, ...]:
    ids = snapshot.task_ids if snapshot else ()
    if not ids:
        return ()
    drop = {index - 1 for index in interp.exclude_indexes if index >= 1}
    return tuple(task_id for index, task_id in enumerate(ids) if index not in drop)


def _drop_indexes(tasks: list[PlanCandidate], indexes: list[int]) -> list[PlanCandidate]:
    drop = {index - 1 for index in indexes if index >= 1}
    if not drop:
        return tasks
    return [task for index, task in enumerate(tasks) if index not in drop]


def _apply_task_filters(
    tasks: list[PlanCandidate], interp: UtteranceInterpretation, today: date
) -> list[PlanCandidate]:
    filtered = tasks
    if interp.include_overdue:
        filtered = filter_overdue(filtered, today)
    elif interp.period and interp.kind != RouterKind.LIST_PERIOD:
        window = resolve_period(interp.period, today)
        if window is not None:
            start, end = window
            filtered = filter_due_between(filtered, start, end)
    return filtered


async def _planning_candidates(queries: TaskQueryService, user_id: int) -> list[PlanCandidate]:
    listed = getattr(queries, "list_planning_candidates", None)
    if listed is not None:
        return await listed(user_id)
    repository = getattr(queries, "_repository", None)
    open_fn = getattr(repository, "list_open_tasks_for_telegram_user", None)
    if open_fn is not None:
        return await open_fn(user_id)
    return await queries.list_open_tasks(user_id)


def _global_list_result(
    tasks: list[PlanCandidate], interp: UtteranceInterpretation
) -> QueryResult:
    if interp.include_overdue and not tasks:
        text = "Немає прострочених задач."
    elif not tasks:
        text = "Немає відкритих задач."
    else:
        groups: dict[str, list[PlanCandidate]] = {}
        for task in tasks:
            groups.setdefault(task.project, []).append(task)
        ordered = tuple(sorted(groups.items(), key=lambda item: item[0].casefold()))
        text = format_all_tasks_grouped(ordered, total=len(tasks))
    summary = sum_estimated_minutes(tasks)
    return QueryResult(
        kind=QueryKind.TASKS if tasks else QueryKind.INFO,
        text=text,
        person_name=None,
        person_query=None,
        task_ids=tuple(task.id for task in tasks),
        titles=tuple(task.title for task in tasks),
        listed_facts=facts_from_tasks(tasks),
        total_estimated_minutes=summary.total_minutes,
        missing_estimates=summary.missing,
        overdue_ids=tuple(task.id for task in tasks) if interp.include_overdue else (),
    )


def _sum_result(tasks: list[PlanCandidate], snapshot: ConversationSnapshot | None) -> QueryResult:
    summary = sum_estimated_minutes(tasks)
    return QueryResult(
        kind=QueryKind.TASKS if tasks else QueryKind.INFO,
        text=_format_sum(summary),
        person_name=snapshot.person_name if snapshot else None,
        person_query=snapshot.person_query if snapshot else None,
        project_name=snapshot.project_name if snapshot else None,
        task_ids=tuple(task.id for task in tasks),
        titles=tuple(task.title for task in tasks),
        listed_facts=facts_from_tasks(tasks),
        total_estimated_minutes=summary.total_minutes,
        missing_estimates=summary.missing,
        period_label=snapshot.last_period if snapshot else None,
        last_available_minutes=snapshot.last_available_minutes if snapshot else None,
    )


def _inspect_result(
    tasks: list[PlanCandidate], snapshot: ConversationSnapshot | None
) -> QueryResult:
    label = (snapshot.person_query or snapshot.person_name or "ці задачі") if snapshot else "ці задачі"
    text = format_person_task_list(label, tasks, empty_query=label)
    compact = "\n".join(f"— {task.title}" for task in tasks) or text
    summary = sum_estimated_minutes(tasks)
    return QueryResult(
        kind=QueryKind.TASKS if tasks else QueryKind.INFO,
        text=compact if tasks else text,
        person_name=snapshot.person_name if snapshot else None,
        person_query=snapshot.person_query if snapshot else None,
        task_ids=tuple(task.id for task in tasks),
        titles=tuple(task.title for task in tasks),
        listed_facts=facts_from_tasks(tasks),
        total_estimated_minutes=summary.total_minutes,
        missing_estimates=summary.missing,
        period_label=snapshot.last_period if snapshot else None,
        last_available_minutes=snapshot.last_available_minutes if snapshot else None,
    )


def _fit_result(
    tasks: list[PlanCandidate],
    budget: int | None,
    *,
    person_name: str | None = None,
) -> QueryResult:
    summary = sum_estimated_minutes(tasks)
    selected = tasks
    used = summary.total_minutes
    if budget and budget > 0:
        fitted, fitted_used = fit_tasks_into_minutes(tasks, budget)
        if fitted:
            selected, used = fitted, fitted_used
        elif summary.missing == 0:
            selected, used = [], 0
    header = _format_fit_header(person_name, selected, summary, used, budget)
    body = "\n".join(
        f"— {task.title}"
        + (f" ({task.estimated_minutes} хв)" if task.estimated_minutes else " (немає оцінки)")
        for task in selected
    )
    text = f"{header}\n{body}" if body else header
    selected_summary = sum_estimated_minutes(selected)
    return QueryResult(
        kind=QueryKind.TASKS if selected else QueryKind.INFO,
        text=text,
        person_name=person_name,
        person_query=person_name,
        task_ids=tuple(task.id for task in selected),
        titles=tuple(task.title for task in selected),
        listed_facts=facts_from_tasks(selected),
        total_estimated_minutes=used if selected else summary.total_minutes,
        missing_estimates=selected_summary.missing,
        last_available_minutes=budget,
    )


def _with_facts(result: QueryResult, tasks: list[PlanCandidate]) -> QueryResult:
    summary = sum_estimated_minutes(tasks)
    return QueryResult(
        kind=result.kind,
        text=result.text,
        person_name=result.person_name,
        project_name=result.project_name,
        task_ids=tuple(task.id for task in tasks) if tasks else result.task_ids,
        titles=tuple(task.title for task in tasks) if tasks else result.titles,
        discuss=result.discuss,
        status_filter=result.status_filter,
        person_query=result.person_query,
        replace_person=result.replace_person,
        listed_facts=facts_from_tasks(tasks) if tasks else result.listed_facts,
        total_estimated_minutes=summary.total_minutes if tasks else result.total_estimated_minutes,
        missing_estimates=summary.missing if tasks else result.missing_estimates,
        period_label=result.period_label,
        overdue_ids=result.overdue_ids,
        last_available_minutes=result.last_available_minutes,
        is_person_ambiguity=result.is_person_ambiguity,
        ambiguity_candidates=result.ambiguity_candidates,
    )


def _format_sum(summary: EstimateSummary) -> str:
    if summary.count == 0:
        return "Немає задач, щоб порахувати час."
    if summary.missing == 0:
        return f"За твоїми оцінками — {summary.total_minutes} хвилин."
    if summary.with_estimates == 0:
        return f"Для {summary.count} задач оцінок немає."
    return (
        f"{summary.with_estimates} задачі мають оцінки на {summary.total_minutes} хвилин; "
        f"для {summary.missing} оцінки немає."
    )


def _format_fit_header(
    label: str | None,
    selected: list[PlanCandidate],
    all_summary: EstimateSummary,
    used: int,
    budget: int | None,
) -> str:
    count = len(selected)
    if not selected:
        if label:
            return f"Зараз немає жодних питань по {label}, тож немає завдань, які можна виконати за цей час."
        return "У доступний час не вміщується жодне відкрите завдання."
    noun = "одне питання" if count == 1 else f"{count} питання"
    if label:
        line = f"У тебе є {noun} по {label}."
    else:
        line = f"У доступний час вміщується {noun}."
    if selected and all_summary.missing == 0:
        line += f" За твоїми оцінками вони займуть {used} хвилин."
        if budget and used <= budget:
            line += f" За {budget} хвилин реально закрити всі {count}."
    elif all_summary.missing:
        line += (
            f" {all_summary.with_estimates} мають оцінки на {all_summary.total_minutes} хвилин; "
            f"для {all_summary.missing} оцінки немає."
        )
    return line


def _format_period(
    period: str,
    start: date,
    end: date,
    due: list[PlanCandidate],
    overdue: list[PlanCandidate],
) -> str:
    label = {
        "next_week": "наступного тижня",
        "this_week": "цього тижня",
        "today": "сьогодні",
        "tomorrow": "завтра",
        "this_month": "цього місяця",
    }.get(period, period)
    range_text = (
        format_uk_date(start)
        if start == end
        else f"{format_uk_date(start)} — {format_uk_date(end)}"
    )
    lines = [f"Дедлайн {label} ({range_text}):"]
    if not due:
        lines.append("Немає задач із дедлайном у цьому періоді.")
    else:
        for task in due:
            extra = f", {task.estimated_minutes} хв" if task.estimated_minutes else ""
            lines.append(f"— {task.title} ({format_uk_date(task.deadline)}{extra})")
    if overdue:
        lines.append("")
        lines.append(
            f"Окремо в тебе є {len(overdue)} прострочені задачі. Вони не входять у цей список."
        )
    return "\n".join(lines)
