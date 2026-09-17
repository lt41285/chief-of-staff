"""Single Telegram → AI conversation, or legacy parsers if no router."""

from datetime import datetime

from chief_of_staff.models.project_command import ProjectIntent, ProjectIntentKind
from chief_of_staff.models.task_command import QUERY_INTENT_KINDS, TaskIntent, TaskIntentKind
from chief_of_staff.models.utterance_intent import QueryRelation, RouterKind, UtteranceInterpretation
from chief_of_staff.services.actual_time import is_actual_time_reply
from chief_of_staff.services.available_time import parse_available_time
from chief_of_staff.services.completion_batch import (
    MIN_BATCH_ITEMS,
    looks_like_all_reference,
    split_completion_items,
)
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.conversation_context import InMemoryConversationStore, PendingAmbiguity
from chief_of_staff.services.daily_planning import DailyPlanningService, PlanResult
from chief_of_staff.services.followup_intent import parse_context_followup
from chief_of_staff.services.grounded_reply import validate_grounded_reply
from chief_of_staff.services.intent_router import (
    ROUTER_UNCLEAR,
    IntentRouter,
    default_chat_reply,
    interpretation_to_draft,
    interpretation_to_intent,
    safe_interpret,
    with_query_text,
)
from chief_of_staff.services.pending_session import (
    PENDING_INTERPRET_FAILED,
    collect_pending_session,
    is_plausible_field_value,
    prefer_read_over_write,
    resolve_pending_control,
)
from chief_of_staff.services.person_ambiguity import parse_person_ambiguity_reply
from chief_of_staff.services.person_resolution import match_name_to_candidates
from chief_of_staff.services.project_intent import parse_project_intent_deterministic
from chief_of_staff.services.project_ops import ProjectManagementService, ProjectResult
from chief_of_staff.services.query_scope import (
    looks_like_informal_address,
    should_reset_query_scope,
)
from chief_of_staff.services.task_intake import IntakeResult, TaskIntakeService
from chief_of_staff.services.task_lifecycle import (
    LifecycleKind,
    LifecycleResult,
    TaskLifecycleService,
)
from chief_of_staff.services.task_query import QueryKind, QueryResult, TaskQueryService
from chief_of_staff.services.tool_runtime import execute_grounded_tools, needs_grounded_tools

UserReply = IntakeResult | PlanResult | ProjectResult | LifecycleResult | QueryResult
_MAX_TOOL_ROUNDS = 2
INFORMAL_ADDRESS_ACK = "Добре, далі говоритиму на ти."


async def process_user_utterance(
    intake: TaskIntakeService,
    user_id: int,
    chat_id: int,
    text: str,
    *,
    planning: DailyPlanningService | None = None,
    projects: ProjectManagementService | None = None,
    lifecycle: TaskLifecycleService | None = None,
    queries: TaskQueryService | None = None,
    router: IntentRouter | None = None,
    context: InMemoryConversationStore | None = None,
    force_intake: bool = False,
) -> UserReply:
    if force_intake:
        return await intake.handle_user_text(user_id, text, chat_id=chat_id)
    if router is not None:
        return await _ai_first_turn(
            intake,
            user_id,
            chat_id,
            text,
            planning=planning,
            projects=projects,
            lifecycle=lifecycle,
            queries=queries,
            router=router,
            context=context,
        )
    if planning is not None and planning.is_awaiting_input(user_id, chat_id):
        return await planning.handle_user_text(user_id, chat_id, text)
    if projects is not None and projects.is_awaiting_input(user_id, chat_id):
        return await projects.handle_user_text(user_id, chat_id, text)
    if lifecycle is not None and lifecycle.is_awaiting_input(user_id, chat_id):
        result = await lifecycle.handle_user_text(user_id, chat_id, text)
        if result.kind == LifecycleKind.DEFER_TO_INTAKE:
            return await intake.handle_user_text(
                user_id,
                result.defer_intake_text or text,
                chat_id=chat_id,
            )
        return result
    if intake.is_awaiting_input(user_id, chat_id):
        return await intake.handle_user_text(user_id, text, chat_id=chat_id)
    return await _legacy_free_text(
        intake,
        user_id,
        chat_id,
        text,
        planning=planning,
        projects=projects,
        lifecycle=lifecycle,
        queries=queries,
        context=context,
    )


async def _ai_first_turn(
    intake: TaskIntakeService,
    user_id: int,
    chat_id: int,
    text: str,
    *,
    planning: DailyPlanningService | None,
    projects: ProjectManagementService | None,
    lifecycle: TaskLifecycleService | None,
    queries: TaskQueryService | None,
    router: IntentRouter,
    context: InMemoryConversationStore | None,
) -> UserReply:
    if context is not None:
        context.append_turn(user_id, chat_id, "user", text)
    snapshot = context.get(user_id, chat_id) if context is not None else None
    now = context.clock.now() if context is not None else datetime.now(KYIV)
    pending = collect_pending_session(
        planning=planning,
        projects=projects,
        lifecycle=lifecycle,
        intake=intake,
        user_id=user_id,
        chat_id=chat_id,
    )
    if (
        pending is not None
        and pending.get("awaiting") == "actual_minutes"
        and lifecycle is not None
        and lifecycle.is_awaiting_input(user_id, chat_id)
        and is_actual_time_reply(text)
    ):
        result = await lifecycle.handle_user_text(user_id, chat_id, text)
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    if (
        pending is not None
        and pending.get("awaiting") in {"task_reference", "which_task"}
        and lifecycle is not None
        and lifecycle.is_awaiting_input(user_id, chat_id)
        and (
            looks_like_all_reference(text)
            or len(split_completion_items(text)) >= MIN_BATCH_ITEMS
        )
    ):
        result = await lifecycle.handle_user_text(user_id, chat_id, text)
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    interp, error = await safe_interpret(
        router, text, snapshot, now=now, pending_session=pending
    )
    if error:
        if pending is not None:
            return QueryResult(kind=QueryKind.INFO, text=PENDING_INTERPRET_FAILED)
        return QueryResult(kind=QueryKind.INFO, text=error)
    assert interp is not None
    interp = prefer_read_over_write(interp, text, pending)
    await _maybe_persist_address(intake, user_id, chat_id, interp, text)
    address_form = await _load_address_form(intake, user_id)

    if snapshot is not None and snapshot.pending_ambiguity is not None and queries is not None:
        leaving_ambiguity = interp.pending_action == "switch_intent" or interp.kind in {
            RouterKind.LIST_ALL_TASKS,
            RouterKind.LIST_PROJECTS,
            RouterKind.LIST_PERIOD,
            RouterKind.LIST_PROJECT_TASKS,
        }
        if leaving_ambiguity:
            pass
        elif interp.kind == RouterKind.CONFIRM_ENTITY_EQUIVALENCE:
            handled = await _apply_person_equivalence(
                queries,
                user_id,
                chat_id,
                text,
                snapshot,
                context,
                canonical=interp.canonical,
                alias=interp.alias,
                pick_name=interp.person_query or interp.replace_person,
                pick_index=interp.task_index,
                now=now,
            )
            if handled is not None:
                return handled
        else:
            handled = await _maybe_resolve_person_ambiguity(
                queries,
                user_id,
                chat_id,
                text,
                snapshot,
                context,
                now=now,
            )
            if handled is not None:
                return handled

    if pending is not None:
        control = resolve_pending_control(interp, pending, text)
        handled = await _execute_pending_control(
            control,
            interp,
            text,
            user_id,
            chat_id,
            pending=pending,
            planning=planning,
            projects=projects,
            lifecycle=lifecycle,
            intake=intake,
            context=context,
            now=now,
        )
        if handled is not None:
            return handled

    if interp.kind == RouterKind.GENERAL_CHAT:
        reply = interp.chat_reply
        if looks_like_informal_address(text) or interp.address_form == "informal":
            reply = INFORMAL_ADDRESS_ACK
        result = QueryResult(
            kind=QueryKind.INFO,
            text=default_chat_reply(text, reply),
        )
        if context is not None:
            context.clear_query_scope(user_id, chat_id)
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    if interp.kind == RouterKind.CONFIRM_ENTITY_EQUIVALENCE and queries is not None:
        handled = await _apply_person_equivalence(
            queries,
            user_id,
            chat_id,
            text,
            snapshot,
            context,
            canonical=interp.canonical,
            alias=interp.alias,
            pick_name=interp.person_query or interp.replace_person,
            pick_index=interp.task_index,
            now=now,
        )
        if handled is not None:
            return handled
    if interp.kind == RouterKind.UNCLEAR:
        return QueryResult(kind=QueryKind.INFO, text=ROUTER_UNCLEAR)
    if interp.kind == RouterKind.LIST_PROJECTS and projects is not None:
        result = await projects.handle_intent(
            user_id,
            chat_id,
            ProjectIntent(kind=ProjectIntentKind.LIST_PROJECTS),
        )
        if context is not None:
            context.clear_query_scope(user_id, chat_id)
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    if interp.kind == RouterKind.LIST_ARCHIVED_PROJECTS and projects is not None:
        result = await projects.handle_intent(
            user_id,
            chat_id,
            ProjectIntent(kind=ProjectIntentKind.LIST_ARCHIVED_PROJECTS),
        )
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    if interp.kind == RouterKind.ARCHIVE_PROJECT and projects is not None:
        result = await projects.handle_intent(
            user_id,
            chat_id,
            ProjectIntent(
                kind=ProjectIntentKind.ARCHIVE_PROJECT,
                project_query=interp.project or interp.project_query,
            ),
        )
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    if interp.kind == RouterKind.RESTORE_PROJECT and projects is not None:
        result = await projects.handle_intent(
            user_id,
            chat_id,
            ProjectIntent(
                kind=ProjectIntentKind.RESTORE_PROJECT,
                project_query=interp.project or interp.project_query,
            ),
        )
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    if interp.kind == RouterKind.DELETE_PROJECT_PERMANENTLY and projects is not None:
        result = await projects.handle_intent(
            user_id,
            chat_id,
            ProjectIntent(
                kind=ProjectIntentKind.DELETE_PROJECT_PERMANENTLY,
                project_query=interp.project or interp.project_query,
            ),
        )
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    if interp.kind == RouterKind.CREATE_PROJECT and projects is not None:
        name = interp.provided_value or interp.project or interp.project_query
        result = await projects.handle_intent(
            user_id,
            chat_id,
            ProjectIntent(kind=ProjectIntentKind.CREATE_PROJECT, new_name=name),
        )
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    if interp.kind == RouterKind.CREATE_TASK:
        result = await intake.handle_user_text(
            user_id,
            text,
            chat_id=chat_id,
            extracted=interpretation_to_draft(interp, today=now.date()),
        )
        if context is not None:
            context.clear_query_scope(user_id, chat_id)
        _remember_assistant(context, user_id, chat_id, result.text)
        return result

    if (
        interp.kind == RouterKind.FIT_MINUTES
        and interp.query_relation == QueryRelation.NEW_INTENT
        and not interp.person_query
        and planning is not None
    ):
        minutes = interp.available_minutes or parse_available_time(text).minutes
        if minutes:
            planned = await planning.plan_with_minutes(user_id, chat_id, minutes)
            if context is not None:
                context.remember_list(
                    user_id,
                    chat_id,
                    task_ids=planned.task_ids,
                    titles=(),
                    person_name=None,
                    person_query=None,
                    project_name=None,
                    last_query_kind="plan",
                    last_action="plan",
                    last_available_minutes=minutes,
                    replace_person=True,
                    reset_scope=True,
                    clear_pending_ambiguity=True,
                )
            _remember_assistant(context, user_id, chat_id, planned.text)
            return planned

    result: UserReply | None = None
    current = interp
    for round_index in range(_MAX_TOOL_ROUNDS):
        if queries is not None and needs_grounded_tools(current):
            result = await execute_grounded_tools(
                queries, user_id, current, snapshot, today=now.date()
            )
        else:
            mapped = interpretation_to_intent(current)
            if mapped is None:
                return QueryResult(kind=QueryKind.INFO, text=ROUTER_UNCLEAR)
            result = await _dispatch_intent(
                intake,
                user_id,
                chat_id,
                text,
                mapped,
                lifecycle=lifecycle,
                queries=queries,
                context=None,
                excluded=current.exclude_person,
            )
        if round_index == 0 and current.follow_up_tool and queries is not None:
            facts = result.text if isinstance(result, QueryResult) else ""
            nxt, err = await safe_interpret(
                router, text, snapshot, now=now, tool_facts=facts
            )
            if err or nxt is None:
                break
            current = nxt
            continue
        break

    assert result is not None
    if isinstance(result, QueryResult) and context is not None:
        _remember_query(context, user_id, chat_id, result, current, original_text=text)
    phrased = await _maybe_phrase(
        router, text, snapshot, result, now=now, address_form=address_form
    )
    if phrased is not None and isinstance(result, QueryResult):
        result = with_query_text(result, phrased)
    _remember_assistant(context, user_id, chat_id, result.text)
    return result


async def _maybe_phrase(
    router: IntentRouter,
    text: str,
    snapshot: object,
    result: UserReply,
    *,
    now: datetime,
    address_form: str | None = None,
) -> str | None:
    phrase = getattr(router, "phrase", None)
    if phrase is None or not isinstance(result, QueryResult):
        return None
    if result.is_person_ambiguity:
        return None
    allowed = tuple(str(task_id) for task_id in result.task_ids)
    spoken = await phrase(
        text,
        snapshot,
        result.text,
        now=now,
        allowed_task_ids=allowed,
        address_form=address_form,
    )
    if spoken is None:
        return None
    if hasattr(spoken, "message"):
        return validate_grounded_reply(spoken, result)
    if isinstance(spoken, str):
        return spoken
    return None


def _remember_assistant(
    context: InMemoryConversationStore | None,
    user_id: int,
    chat_id: int,
    text: str,
) -> None:
    if context is not None:
        context.append_turn(user_id, chat_id, "assistant", text)


async def _maybe_persist_address(
    intake: TaskIntakeService,
    user_id: int,
    chat_id: int,
    interp: UtteranceInterpretation,
    text: str,
) -> None:
    form = interp.address_form
    if looks_like_informal_address(text):
        form = "informal"
    if not form:
        return
    setter = getattr(intake._repository, "set_address_form", None)
    if setter is None:
        return
    await setter(user_id, form, telegram_chat_id=chat_id)


async def _load_address_form(intake: TaskIntakeService, user_id: int) -> str | None:
    getter = getattr(intake._repository, "get_address_form", None)
    if getter is None:
        return None
    return await getter(user_id)


def _clear_pending_sessions(
    *,
    planning: DailyPlanningService | None,
    projects: ProjectManagementService | None,
    lifecycle: TaskLifecycleService | None,
    intake: TaskIntakeService,
    user_id: int,
    chat_id: int,
) -> UserReply | None:
    last: UserReply | None = None
    if planning is not None and planning.is_awaiting_input(user_id, chat_id):
        last = planning.cancel(user_id, chat_id)
    if projects is not None and projects.is_awaiting_input(user_id, chat_id):
        last = projects.cancel(user_id, chat_id)
    if lifecycle is not None and lifecycle.is_awaiting_input(user_id, chat_id):
        last = lifecycle.cancel(user_id, chat_id)
    if intake.is_awaiting_input(user_id, chat_id):
        last = intake.cancel(user_id, chat_id)
    return last


async def _feed_pending_value(
    *,
    planning: DailyPlanningService | None,
    projects: ProjectManagementService | None,
    lifecycle: TaskLifecycleService | None,
    intake: TaskIntakeService,
    user_id: int,
    chat_id: int,
    value: str,
) -> UserReply:
    if planning is not None and planning.is_awaiting_input(user_id, chat_id):
        return await planning.handle_user_text(user_id, chat_id, value)
    if projects is not None and projects.is_awaiting_input(user_id, chat_id):
        return await projects.handle_user_text(user_id, chat_id, value)
    if lifecycle is not None and lifecycle.is_awaiting_input(user_id, chat_id):
        result = await lifecycle.handle_user_text(user_id, chat_id, value)
        if result.kind == LifecycleKind.DEFER_TO_INTAKE:
            return await intake.handle_user_text(
                user_id,
                result.defer_intake_text or value,
                chat_id=chat_id,
            )
        return result
    return await intake.handle_user_text(user_id, value, chat_id=chat_id)


async def _execute_pending_control(
    control: str | None,
    interp: UtteranceInterpretation,
    text: str,
    user_id: int,
    chat_id: int,
    *,
    pending: dict,
    planning: DailyPlanningService | None,
    projects: ProjectManagementService | None,
    lifecycle: TaskLifecycleService | None,
    intake: TaskIntakeService,
    context: InMemoryConversationStore | None,
    now: datetime,
) -> UserReply | None:
    if control == "cancel":
        result = _clear_pending_sessions(
            planning=planning,
            projects=projects,
            lifecycle=lifecycle,
            intake=intake,
            user_id=user_id,
            chat_id=chat_id,
        )
        if result is None:
            return QueryResult(kind=QueryKind.INFO, text=PENDING_INTERPRET_FAILED)
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    if control == "retry":
        if pending.get("awaiting") == "actual_minutes" and lifecycle is not None:
            # The service re-asks once, then lets the chain move on.
            result = await lifecycle.handle_user_text(user_id, chat_id, text)
            _remember_assistant(context, user_id, chat_id, result.text)
            return result
        return QueryResult(kind=QueryKind.INFO, text=PENDING_INTERPRET_FAILED)
    if control == "keep_chat":
        result = QueryResult(
            kind=QueryKind.INFO,
            text=default_chat_reply(text, interp.chat_reply),
        )
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    if control == "continue":
        if interp.skip_actual_minutes and lifecycle is not None:
            fed = await lifecycle.handle_user_text(user_id, chat_id, "пропустити")
            _remember_assistant(context, user_id, chat_id, fed.text)
            return fed
        value = interp.provided_value or text
        if not is_plausible_field_value(pending, value) and not interp.skip_actual_minutes:
            return QueryResult(kind=QueryKind.INFO, text=PENDING_INTERPRET_FAILED)
        fed = await _feed_pending_value(
            planning=planning,
            projects=projects,
            lifecycle=lifecycle,
            intake=intake,
            user_id=user_id,
            chat_id=chat_id,
            value=value,
        )
        _remember_assistant(context, user_id, chat_id, fed.text)
        return fed
    if control == "provide":
        value = text if pending.get("awaiting") == "actual_minutes" else (
            interp.provided_value or interp.project or interp.project_query or text
        )
        if not is_plausible_field_value(pending, value):
            return QueryResult(kind=QueryKind.INFO, text=PENDING_INTERPRET_FAILED)
        fed = await _feed_pending_value(
            planning=planning,
            projects=projects,
            lifecycle=lifecycle,
            intake=intake,
            user_id=user_id,
            chat_id=chat_id,
            value=value,
        )
        _remember_assistant(context, user_id, chat_id, fed.text)
        return fed
    if control == "patch":
        extracted = interpretation_to_draft(interp, today=now.date())
        fed = await intake.handle_user_text(
            user_id,
            text,
            chat_id=chat_id,
            extracted=extracted,
        )
        _remember_assistant(context, user_id, chat_id, fed.text)
        return fed
    if control == "switch":
        _clear_pending_sessions(
            planning=planning,
            projects=projects,
            lifecycle=lifecycle,
            intake=intake,
            user_id=user_id,
            chat_id=chat_id,
        )
        return None
    return QueryResult(kind=QueryKind.INFO, text=PENDING_INTERPRET_FAILED)


async def _maybe_resolve_person_ambiguity(
    queries: TaskQueryService,
    user_id: int,
    chat_id: int,
    text: str,
    snapshot: object,
    context: InMemoryConversationStore | None,
    *,
    now: datetime,
) -> QueryResult | None:
    pending = getattr(snapshot, "pending_ambiguity", None)
    action = parse_person_ambiguity_reply(text, pending)
    if action is None:
        return None
    return await _apply_person_equivalence(
        queries,
        user_id,
        chat_id,
        text,
        snapshot,
        context,
        canonical=str(action.get("canonical") or ""),
        alias=str(action.get("alias") or ""),
        pick_name=str(action["name"]) if action.get("action") == "pick" else None,
        now=now,
        merge=action.get("action") == "merge",
    )


async def _apply_person_equivalence(
    queries: TaskQueryService,
    user_id: int,
    chat_id: int,
    text: str,
    snapshot: object,
    context: InMemoryConversationStore | None,
    *,
    canonical: str | None,
    alias: str | None,
    pick_name: str | None = None,
    pick_index: int | None = None,
    now: datetime,
    merge: bool = True,
) -> QueryResult | None:
    pending = getattr(snapshot, "pending_ambiguity", None)
    if pending is None:
        return QueryResult(
            kind=QueryKind.INFO,
            text="Немає відкритого уточнення, яке можна зберегти як одне ім'я.",
        )
    chosen = pick_name
    if chosen is None and pick_index:
        pos = pick_index - 1
        if 0 <= pos < len(pending.candidates):
            chosen = pending.candidates[pos]
    if chosen:
        resolved_name = match_name_to_candidates(chosen, pending.candidates) or chosen
        follow = await _rerun_person_query(queries, user_id, pending, resolved_name, now.date())
        if context is not None:
            _remember_query(
                context,
                user_id,
                chat_id,
                follow,
                UtteranceInterpretation(
                    kind=RouterKind.PEOPLE_TASKS_QUERY, person_query=resolved_name
                ),
                original_text=pending.original_message,
            )
            _remember_assistant(context, user_id, chat_id, follow.text)
        return follow
    if not merge or not canonical or not alias:
        return None
    canon_hit = match_name_to_candidates(canonical, pending.candidates)
    alias_hit = match_name_to_candidates(alias, pending.candidates)
    if canon_hit is None or alias_hit is None:
        return QueryResult(
            kind=QueryKind.INFO,
            text="Уточнення має стосуватися тих імен, які я щойно запропонував.",
        )
    outcome = await queries.confirm_person_equivalence(user_id, canon_hit, alias_hit)
    if outcome.status == "conflict":
        text_out = (
            f"«{outcome.alias}» уже означає «{outcome.conflict_with}», "
            f"а не «{outcome.canonical_name}». Кого залишаємо?"
        )
        result = QueryResult(kind=QueryKind.INFO, text=text_out, is_person_ambiguity=True,
                             ambiguity_candidates=pending.candidates, person_query=pending.person_query)
        _remember_assistant(context, user_id, chat_id, result.text)
        return result
    if outcome.status != "ok":
        return QueryResult(kind=QueryKind.INFO, text="Не знайшов ці імена серед твоїх людей.")
    follow = await _rerun_person_query(
        queries, user_id, pending, outcome.canonical_name, now.date()
    )
    header = f"Добре. Надалі «{outcome.alias}» = «{outcome.canonical_name}»."
    combined = with_query_text(follow, f"{header}\n{follow.text}")
    if context is not None:
        _remember_query(
            context,
            user_id,
            chat_id,
            combined,
            UtteranceInterpretation(
                kind=RouterKind.PEOPLE_TASKS_QUERY, person_query=outcome.canonical_name
            ),
            original_text=pending.original_message,
        )
        _remember_assistant(context, user_id, chat_id, combined.text)
    return combined


async def _rerun_person_query(
    queries: TaskQueryService,
    user_id: int,
    pending: PendingAmbiguity,
    person_query: str,
    today,
):
    interp = UtteranceInterpretation(
        kind=RouterKind.PEOPLE_TASKS_QUERY,
        person_query=person_query,
        available_minutes=pending.available_minutes,
        status_filter=pending.status_filter,
        project_query=pending.project_query,
        discuss=pending.discuss,
    )
    return await execute_grounded_tools(queries, user_id, interp, None, today=today)


def _remember_query(
    context: InMemoryConversationStore,
    user_id: int,
    chat_id: int,
    result: QueryResult,
    interp: object,
    *,
    original_text: str | None = None,
) -> None:
    period = getattr(interp, "period", None) or result.period_label
    available = getattr(interp, "available_minutes", None) or result.last_available_minutes
    pending = None
    if result.is_person_ambiguity and result.ambiguity_candidates:
        pending = PendingAmbiguity(
            original_message=original_text or "",
            original_reference=result.person_query or "",
            candidates=result.ambiguity_candidates,
            intent_kind=str(getattr(getattr(interp, "kind", None), "value", "") or "people_tasks_query"),
            person_query=result.person_query or "",
            available_minutes=available,
            status_filter=result.status_filter,
            project_query=result.project_name,
            discuss=result.discuss,
        )
    context.remember_list(
        user_id,
        chat_id,
        task_ids=result.task_ids,
        titles=result.titles,
        person_name=result.person_name,
        person_query=result.person_query,
        project_name=result.project_name,
        status_filter=result.status_filter,
        discuss=result.discuss,
        last_query_kind=getattr(getattr(interp, "kind", None), "value", None),
        last_action=getattr(getattr(interp, "kind", None), "value", None),
        excluded_people=(getattr(interp, "exclude_person", None),)
        if getattr(interp, "exclude_person", None)
        else None,
        replace_person=result.replace_person
        or bool(getattr(interp, "exclude_person", None))
        or bool(getattr(interp, "clear_person_filter", False))
        or should_reset_query_scope(interp),
        listed_facts=result.listed_facts,
        last_period=period,
        last_available_minutes=available,
        last_exclude_overdue=bool(getattr(interp, "exclude_overdue", False)),
        last_include_overdue=bool(getattr(interp, "include_overdue", False)),
        pending_ambiguity=pending,
        clear_pending_ambiguity=pending is None and result.is_person_ambiguity is False,
        reset_scope=should_reset_query_scope(interp),
    )


async def _legacy_free_text(
    intake: TaskIntakeService,
    user_id: int,
    chat_id: int,
    text: str,
    *,
    planning: DailyPlanningService | None,
    projects: ProjectManagementService | None,
    lifecycle: TaskLifecycleService | None,
    queries: TaskQueryService | None,
    context: InMemoryConversationStore | None,
) -> UserReply:
    if projects is not None:
        create = parse_project_intent_deterministic(text)
        if create is not None and create.kind == ProjectIntentKind.CREATE_PROJECT:
            return await projects.handle_intent(user_id, chat_id, create)
        if create is not None and create.kind in {
            ProjectIntentKind.ARCHIVE_PROJECT,
            ProjectIntentKind.RESTORE_PROJECT,
            ProjectIntentKind.LIST_ARCHIVED_PROJECTS,
            ProjectIntentKind.DELETE_PROJECT_PERMANENTLY,
            ProjectIntentKind.LIST_PROJECTS,
        }:
            return await projects.handle_intent(user_id, chat_id, create)
    snapshot = context.get(user_id, chat_id) if context is not None else None
    today = context.clock.now().date() if context is not None else datetime.now(KYIV).date()
    follow = parse_context_followup(text, snapshot, today=today)
    if follow is not None:
        return await _dispatch_intent(
            intake,
            user_id,
            chat_id,
            text,
            follow,
            lifecycle=lifecycle,
            queries=queries,
            context=context,
        )
    if queries is not None:
        query_intent = await queries.classify(text)
        if query_intent is not None:
            return await _dispatch_intent(
                intake,
                user_id,
                chat_id,
                text,
                query_intent,
                lifecycle=lifecycle,
                queries=queries,
                context=context,
            )
    if lifecycle is not None:
        task_intent = await lifecycle.classify(text)
        if task_intent.kind in QUERY_INTENT_KINDS and queries is not None:
            return await _dispatch_intent(
                intake,
                user_id,
                chat_id,
                text,
                task_intent,
                lifecycle=lifecycle,
                queries=queries,
                context=context,
            )
        if task_intent.kind != TaskIntentKind.NORMAL_TASK_INPUT:
            return await _dispatch_intent(
                intake,
                user_id,
                chat_id,
                text,
                task_intent,
                lifecycle=lifecycle,
                queries=queries,
                context=context,
            )
        statement = await lifecycle.consider_completed_statement(user_id, chat_id, text)
        if statement is not None:
            return statement
    if projects is not None:
        intent = await projects.classify(text)
        if intent.kind != ProjectIntentKind.CREATE_TASK:
            return await projects.handle_intent(user_id, chat_id, intent)
    return await intake.handle_user_text(user_id, text, chat_id=chat_id)


async def _dispatch_intent(
    intake: TaskIntakeService,
    user_id: int,
    chat_id: int,
    text: str,
    intent: TaskIntent,
    *,
    lifecycle: TaskLifecycleService | None,
    queries: TaskQueryService | None,
    context: InMemoryConversationStore | None,
    excluded: str | None = None,
) -> UserReply:
    if excluded:
        intent = intent.model_copy(update={"exclude_person": excluded})
    if intent.kind in QUERY_INTENT_KINDS and queries is not None:
        result = await queries.handle_intent(user_id, intent)
        if context is not None:
            clear_person = intent.kind == TaskIntentKind.LIST_ALL_TASKS
            context.remember_list(
                user_id,
                chat_id,
                task_ids=result.task_ids,
                titles=result.titles,
                person_name=None if clear_person else result.person_name,
                person_query=None if clear_person else (result.person_query or intent.person_query),
                project_name=result.project_name,
                status_filter=result.status_filter,
                discuss=result.discuss,
                last_query_kind=intent.kind.value,
                last_action=intent.kind.value,
                excluded_people=(intent.exclude_person,) if intent.exclude_person else None,
                replace_person=clear_person or result.replace_person or bool(intent.exclude_person),
                listed_facts=result.listed_facts,
            )
        return result
    if intent.kind != TaskIntentKind.NORMAL_TASK_INPUT and lifecycle is not None:
        return await lifecycle.handle_intent(user_id, chat_id, intent, raw_text=text)
    return await intake.handle_user_text(user_id, text, chat_id=chat_id)
