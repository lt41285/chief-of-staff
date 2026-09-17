"""AI conversation brain. Python still executes tools and writes."""

from datetime import date, datetime

from loguru import logger

from chief_of_staff.models.task import TaskDraft, TaskExtractionSchema
from chief_of_staff.models.task_command import TaskIntent, TaskIntentKind
from chief_of_staff.models.utterance_intent import (
    GroundedReply,
    RouterKind,
    UtteranceInterpretation,
)
from chief_of_staff.services.query_scope import apply_query_scope, clean_person_query
from chief_of_staff.prompts.conversation import CONVERSATION_INSTRUCTIONS
from chief_of_staff.services.conversation_context import (
    ConversationSnapshot,
    format_snapshot_for_prompt,
)
from chief_of_staff.services.task_query import QueryResult
from chief_of_staff.services.task_validation import draft_from_extraction, format_kyiv_clock

ROUTER_FAILED = "Не вдалося розібрати повідомлення. Спробуй ще раз."
ROUTER_UNCLEAR = "Не зовсім зрозумів. Напиши, що саме показати або що зробити."

_KIND_MAP = {
    RouterKind.PEOPLE_TASKS_QUERY: TaskIntentKind.PEOPLE_TASKS_QUERY,
    RouterKind.LIST_PROJECT_TASKS: TaskIntentKind.LIST_PROJECT_TASKS,
    RouterKind.LIST_ALL_TASKS: TaskIntentKind.LIST_ALL_TASKS,
    RouterKind.COMPLETE_TASK: TaskIntentKind.COMPLETE_TASK,
    RouterKind.COMPLETE_STATEMENT: TaskIntentKind.COMPLETE_TASK,
    RouterKind.POSTPONE_TASK: TaskIntentKind.POSTPONE_TASK,
    RouterKind.WAITING_TASK: TaskIntentKind.WAITING_TASK,
    RouterKind.RESUME_TASK: TaskIntentKind.RESUME_TASK,
    RouterKind.CREATE_TASK: TaskIntentKind.NORMAL_TASK_INPUT,
    RouterKind.RETRY_PREVIOUS: TaskIntentKind.PEOPLE_TASKS_QUERY,
    RouterKind.CORRECT_ENTITY: TaskIntentKind.PEOPLE_TASKS_QUERY,
}


class IntentRouter:
    def __init__(self, client: object) -> None:
        self._client = client

    async def interpret(
        self,
        text: str,
        snapshot: ConversationSnapshot | None,
        *,
        now: datetime,
        tool_facts: str | None = None,
        pending_session: dict | None = None,
    ) -> UtteranceInterpretation:
        from chief_of_staff.services.pending_session import format_pending_for_prompt

        parts = [
            f"Current date/time in Europe/Kyiv: {format_kyiv_clock(now)}",
            format_snapshot_for_prompt(snapshot),
            format_pending_for_prompt(pending_session),
            f"User message:\n{text}",
        ]
        if tool_facts:
            parts.append(
                "Python tool facts from this turn (authoritative). "
                "You may choose one more tool or stop.\n"
                f"{tool_facts}"
            )
        return await self._client.parse_structured(  # type: ignore[attr-defined]
            user_input="\n\n".join(parts),
            instructions=CONVERSATION_INSTRUCTIONS,
            text_format=UtteranceInterpretation,
        )

    async def phrase(
        self,
        text: str,
        snapshot: ConversationSnapshot | None,
        facts: str,
        *,
        now: datetime,
        allowed_task_ids: tuple[str, ...] = (),
        address_form: str | None = None,
    ) -> GroundedReply | None:
        phrase = getattr(self._client, "parse_structured", None)
        if phrase is None:
            return None
        try:
            from chief_of_staff.prompts.conversation import phrase_instructions_for

            result = await self._client.parse_structured(  # type: ignore[attr-defined]
                user_input="\n\n".join(
                    [
                        f"Current date/time in Europe/Kyiv: {format_kyiv_clock(now)}",
                        format_snapshot_for_prompt(snapshot),
                        f"User message:\n{text}",
                        f"Allowed task IDs: {list(allowed_task_ids)}",
                        f"FACTS:\n{facts}",
                    ]
                ),
                instructions=phrase_instructions_for(address_form),
                text_format=GroundedReply,
            )
        except Exception:
            logger.exception("Reply phrasing failed")
            return None
        if not isinstance(result, GroundedReply):
            reply = getattr(result, "reply", None) or getattr(result, "message", None)
            if not reply:
                return None
            return GroundedReply(message=str(reply).strip())
        if not (result.message or "").strip():
            return None
        return result


def apply_context(
    interp: UtteranceInterpretation,
    snapshot: ConversationSnapshot | None,
    text: str = "",
) -> UtteranceInterpretation:
    scoped = apply_query_scope(interp, snapshot, text)
    if snapshot is None:
        return scoped
    updates: dict[str, object] = {}
    if scoped.kind in {RouterKind.SUM_ESTIMATES, RouterKind.INSPECT_LISTED} and snapshot.task_ids:
        updates["use_listed_ids"] = True
    if scoped.task_index and not scoped.task_query and snapshot.titles:
        pos = scoped.task_index - 1
        if 0 <= pos < len(snapshot.titles):
            updates["task_query"] = snapshot.titles[pos]
    if scoped.kind in {RouterKind.POSTPONE_TASK, RouterKind.COMPLETE_TASK, RouterKind.WAITING_TASK}:
        if scoped.task_index and snapshot.titles:
            pos = scoped.task_index - 1
            if 0 <= pos < len(snapshot.titles):
                updates["task_query"] = snapshot.titles[pos]
    if updates:
        return scoped.model_copy(update=updates)
    return scoped


def interpretation_to_intent(interp: UtteranceInterpretation) -> TaskIntent | None:
    kind = _KIND_MAP.get(interp.kind)
    if kind is None:
        return None
    task_query = interp.task_query
    if interp.kind == RouterKind.PEOPLE_TASKS_QUERY and interp.discuss:
        task_query = "discuss"
    return TaskIntent(
        kind=kind,
        task_query=task_query,
        project_query=interp.project_query,
        status_filter=interp.status_filter,
        person_query=clean_person_query(interp.person_query),
        new_deadline=interp.deadline_on or interp.deadline,
        waiting_for=interp.waiting_for,
        task_index=interp.task_index,
        deadline_on=interp.deadline_on,
        exclude_person=interp.exclude_person,
        broader_search=interp.broader_search or interp.retry_previous,
    )


def interpretation_to_draft(
    interp: UtteranceInterpretation, *, today: date | None = None
) -> TaskDraft:
    schema = TaskExtractionSchema(
        task_title=interp.task_title,
        project=interp.project or interp.project_query,
        people=list(interp.people),
        deadline=interp.deadline or interp.deadline_on,
        estimated_minutes=interp.estimated_minutes,
        desired_outcome=interp.desired_outcome,
    )
    return draft_from_extraction(schema, today=today)


def default_chat_reply(text: str, suggested: str | None = None) -> str:
    if suggested and suggested.strip():
        return suggested.strip()
    folded = text.strip().casefold()
    if folded in {"привіт", "вітаю", "hello", "hi", "hey"}:
        return "Привіт."
    if folded in {"дякую", "спасибі", "thanks", "thank you"}:
        return "Будь ласка."
    if folded in {"ок", "окей", "ok", "okay", "зрозуміло", "ясно"}:
        return "Ок."
    if folded in {"хмм", "хм", "hmm"}:
        return "Так, слухаю."
    return "Так, слухаю."


def with_query_text(result: QueryResult, text: str) -> QueryResult:
    return QueryResult(
        kind=result.kind,
        text=text,
        person_name=result.person_name,
        project_name=result.project_name,
        task_ids=result.task_ids,
        titles=result.titles,
        discuss=result.discuss,
        status_filter=result.status_filter,
        person_query=result.person_query,
        replace_person=result.replace_person,
        listed_facts=result.listed_facts,
        total_estimated_minutes=result.total_estimated_minutes,
        missing_estimates=result.missing_estimates,
        period_label=result.period_label,
        overdue_ids=result.overdue_ids,
        last_available_minutes=result.last_available_minutes,
        is_person_ambiguity=result.is_person_ambiguity,
        ambiguity_candidates=result.ambiguity_candidates,
    )


async def safe_interpret(
    router: IntentRouter,
    text: str,
    snapshot: ConversationSnapshot | None,
    *,
    now: datetime,
    tool_facts: str | None = None,
    pending_session: dict | None = None,
) -> tuple[UtteranceInterpretation | None, str | None]:
    try:
        interp = await router.interpret(
            text,
            snapshot,
            now=now,
            tool_facts=tool_facts,
            pending_session=pending_session,
        )
    except TypeError:
        try:
            interp = await router.interpret(text, snapshot, now=now, tool_facts=tool_facts)
        except TypeError:
            try:
                interp = await router.interpret(text, snapshot, now=now)
            except Exception:
                logger.exception("Central intent router failed")
                return None, ROUTER_FAILED
        except Exception:
            logger.exception("Central intent router failed")
            return None, ROUTER_FAILED
    except Exception:
        logger.exception("Central intent router failed")
        return None, ROUTER_FAILED
    return apply_context(interp, snapshot, text), None
