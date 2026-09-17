"""Pending mutation sessions are context for AI, not a free-text sink."""

from __future__ import annotations

import json
import re
from typing import Any

from chief_of_staff.models.utterance_intent import RouterKind, UtteranceInterpretation
from chief_of_staff.services.actual_time import (
    is_actual_time_reply,
    is_skip_actual_time,
    parse_actual_minutes,
)
from chief_of_staff.services.project_intent import (
    looks_like_explicit_project_create,
    looks_like_list_archived_projects,
    looks_like_list_projects,
)
from chief_of_staff.services.task_command_intent import parse_task_intent_deterministic

PENDING_INTERPRET_FAILED = (
    "Не вдалося зрозуміти відповідь. Спробуй ще раз або натисни Скасувати."
)

READ_KINDS = frozenset(
    {
        RouterKind.LIST_PROJECTS,
        RouterKind.LIST_ARCHIVED_PROJECTS,
        RouterKind.LIST_ALL_TASKS,
        RouterKind.LIST_PROJECT_TASKS,
        RouterKind.PEOPLE_TASKS_QUERY,
        RouterKind.INSPECT_LISTED,
        RouterKind.SUM_ESTIMATES,
        RouterKind.FIT_MINUTES,
        RouterKind.LIST_PERIOD,
        RouterKind.REFINE_PREVIOUS,
        RouterKind.RETRY_PREVIOUS,
        RouterKind.CORRECT_ENTITY,
    }
)
WRITE_KINDS = frozenset(
    {
        RouterKind.CREATE_TASK,
        RouterKind.CREATE_PROJECT,
        RouterKind.ARCHIVE_PROJECT,
        RouterKind.RESTORE_PROJECT,
        RouterKind.DELETE_PROJECT_PERMANENTLY,
        RouterKind.COMPLETE_TASK,
        RouterKind.COMPLETE_STATEMENT,
        RouterKind.POSTPONE_TASK,
        RouterKind.WAITING_TASK,
        RouterKind.RESUME_TASK,
    }
)
CONTROL_KINDS = frozenset(
    {
        RouterKind.PROVIDE_PENDING_VALUE,
        RouterKind.CANCEL_PENDING,
        RouterKind.CONTINUE_PENDING,
    }
)

_CANCEL_ACTIONS = frozenset({"cancel_pending_action"})
_PROVIDE_ACTIONS = frozenset({"provide_requested_value", "correct_pending_action"})
_SWITCH_ACTIONS = frozenset({"switch_intent"})
_CONTINUE_ACTIONS = frozenset({"continue_pending_action"})


def format_pending_for_prompt(pending: dict[str, Any] | None) -> str:
    if not pending:
        return "pending_session: none"
    payload = json.dumps(pending, ensure_ascii=False, default=str)
    return (
        "pending_session (context only; do not assume the utterance is the awaited field):\n"
        f"{payload}\n"
        "The user may answer, reject, correct, switch intent, ask something else, or cancel."
    )


def collect_pending_session(
    *,
    planning: Any | None,
    projects: Any | None,
    lifecycle: Any | None,
    intake: Any | None,
    user_id: int,
    chat_id: int,
) -> dict[str, Any] | None:
    for service in (planning, projects, lifecycle, intake):
        if service is None:
            continue
        describe = getattr(service, "describe_pending", None)
        if describe is None:
            continue
        view = describe(user_id, chat_id)
        if view:
            return view
    return None


def prefer_read_over_write(
    interp: UtteranceInterpretation,
    text: str,
    pending: dict[str, Any] | None,
) -> UtteranceInterpretation:
    if looks_like_list_archived_projects(text):
        action = "switch_intent" if pending else interp.pending_action
        return interp.model_copy(
            update={
                "kind": RouterKind.LIST_ARCHIVED_PROJECTS,
                "pending_action": action,
                "provided_value": None,
            }
        )
    if looks_like_list_projects(text) and not looks_like_explicit_project_create(text):
        action = "switch_intent" if pending else interp.pending_action
        return interp.model_copy(
            update={
                "kind": RouterKind.LIST_PROJECTS,
                "pending_action": action,
                "provided_value": None,
            }
        )
    folded = " ".join(text.split()).casefold()
    if (
        pending
        and pending.get("pending_action") == "create_project"
        and "загальний список" in folded
        and not looks_like_explicit_project_create(text)
    ):
        return interp.model_copy(
            update={
                "kind": RouterKind.LIST_PROJECTS,
                "pending_action": "switch_intent",
                "provided_value": None,
            }
        )
    if interp.kind == RouterKind.CREATE_PROJECT and not looks_like_explicit_project_create(text):
        if pending:
            return interp.model_copy(
                update={
                    "kind": RouterKind.UNCLEAR,
                    "pending_action": None,
                    "provided_value": None,
                }
            )
        return interp.model_copy(update={"kind": RouterKind.UNCLEAR})
    return interp


def resolve_pending_control(
    interp: UtteranceInterpretation,
    pending: dict[str, Any] | None,
    text: str,
) -> str | None:
    """Return cancel | provide | continue | switch | keep_chat | retry, or None."""
    if pending is None:
        return None
    action = (interp.pending_action or "").strip()
    if _awaiting_actual_minutes(pending):
        if interp.kind == RouterKind.CANCEL_PENDING or action in _CANCEL_ACTIONS:
            return "cancel"
        if is_skip_actual_time(text) or interp.skip_actual_minutes:
            return "continue"
        if parse_actual_minutes(text, allow_bare=True) is not None:
            return "provide"
        if interp.kind in READ_KINDS:
            return "switch"
        if interp.kind in WRITE_KINDS and interp.kind not in {
            RouterKind.COMPLETE_TASK,
            RouterKind.COMPLETE_STATEMENT,
            RouterKind.PROVIDE_PENDING_VALUE,
            RouterKind.CONTINUE_PENDING,
        }:
            return "switch"
        restart = parse_task_intent_deterministic(text)
        if restart is not None and not is_actual_time_reply(text):
            return "switch"
        return "retry"
    if pending.get("pending_action") == "create_task":
        if interp.kind == RouterKind.CANCEL_PENDING or action in _CANCEL_ACTIONS:
            return "cancel"
        if interp.kind == RouterKind.GENERAL_CHAT or action == "general_chat":
            return "keep_chat"
        if interp.kind in READ_KINDS or action in _SWITCH_ACTIONS:
            return "switch"
        if interp.kind in WRITE_KINDS and interp.kind != RouterKind.CREATE_TASK:
            return "switch"
        return "patch"
    if interp.kind == RouterKind.CANCEL_PENDING or action in _CANCEL_ACTIONS:
        return "cancel"
    if interp.kind == RouterKind.CONTINUE_PENDING or action in _CONTINUE_ACTIONS:
        return "continue"
    if interp.kind in READ_KINDS or action in _SWITCH_ACTIONS:
        return "switch"
    if interp.kind in {RouterKind.CREATE_TASK, RouterKind.COMPLETE_TASK, RouterKind.COMPLETE_STATEMENT,
                       RouterKind.POSTPONE_TASK, RouterKind.WAITING_TASK, RouterKind.RESUME_TASK} and not (
        pending.get("pending_action") in {"complete_task", "postpone_task", "waiting_task", "resume_task", "create_task"}
        and interp.kind.value.replace("complete_statement", "complete_task") == pending.get("pending_action")
    ):
        if interp.kind == RouterKind.CREATE_TASK and pending.get("pending_action") == "create_task":
            pass
        else:
            same = _same_mutation(interp, pending)
            if not same:
                return "switch"
    if interp.kind == RouterKind.PROVIDE_PENDING_VALUE or action in _PROVIDE_ACTIONS:
        return "provide"
    if interp.kind == RouterKind.CREATE_PROJECT and pending.get("pending_action") == "create_project":
        value = interp.provided_value or interp.project or interp.project_query
        if value and is_plausible_field_value(pending, value):
            return "provide"
        if is_plausible_field_value(pending, text) and looks_like_explicit_project_create(text) is False:
            if _looks_like_short_name(text):
                return "provide"
        return "retry"
    if interp.kind == RouterKind.GENERAL_CHAT or action == "general_chat":
        return "keep_chat"
    if interp.kind == RouterKind.UNCLEAR:
        return "retry"
    if interp.kind in WRITE_KINDS:
        same = _same_mutation(interp, pending)
        if same and is_plausible_field_value(pending, interp.provided_value or text):
            return "provide"
        return "switch"
    return "retry"


def is_plausible_field_value(pending: dict[str, Any], value: str | None) -> bool:
    if not value or not value.strip():
        return False
    compact = " ".join(value.split())
    awaiting = str(pending.get("awaiting") or "")
    if looks_like_list_projects(compact):
        return False
    if awaiting in {"project_name", "project"}:
        return _looks_like_short_name(compact)
    if awaiting in {"deadline", "new_deadline"}:
        return len(compact) <= 40 and len(compact.split()) <= 6
    if awaiting == "actual_minutes":
        return is_actual_time_reply(compact)
    if awaiting == "strong_confirmation":
        compact = compact.replace(" ", "").casefold()
        return compact == "видалитиназавжди"
    if awaiting == "confirmation":
        return compact.casefold() in {"так", "yes", "y", "ні", "нет", "no", "скасувати", "cancel"}
    if len(compact) > 120 or len(compact.split()) > 20:
        return False
    return True


def _looks_like_short_name(text: str) -> bool:
    compact = " ".join(text.split())
    if looks_like_list_projects(compact):
        return False
    if len(compact) > 80 or len(compact.split()) > 8:
        return False
    if re.search(r"(?i)\b(не\s+створ|скасуй|покажи|список|задач)\b", compact):
        return False
    return True


def _same_mutation(interp: UtteranceInterpretation, pending: dict[str, Any]) -> bool:
    wanted = str(pending.get("pending_action") or "")
    kind = interp.kind
    if wanted == "create_project":
        return kind in {RouterKind.CREATE_PROJECT, RouterKind.PROVIDE_PENDING_VALUE}
    if wanted == "archive_project":
        return kind in {RouterKind.ARCHIVE_PROJECT, RouterKind.PROVIDE_PENDING_VALUE, RouterKind.CONTINUE_PENDING}
    if wanted == "restore_project":
        return kind in {RouterKind.RESTORE_PROJECT, RouterKind.PROVIDE_PENDING_VALUE, RouterKind.CONTINUE_PENDING}
    if wanted == "delete_project_permanently":
        return kind in {
            RouterKind.DELETE_PROJECT_PERMANENTLY,
            RouterKind.PROVIDE_PENDING_VALUE,
            RouterKind.CONTINUE_PENDING,
        }
    if wanted == "create_task":
        return kind in {RouterKind.CREATE_TASK, RouterKind.PROVIDE_PENDING_VALUE}
    if wanted == "postpone_task":
        return kind in {RouterKind.POSTPONE_TASK, RouterKind.PROVIDE_PENDING_VALUE}
    if wanted in {"complete_task"}:
        return kind in {
            RouterKind.COMPLETE_TASK,
            RouterKind.COMPLETE_STATEMENT,
            RouterKind.CONTINUE_PENDING,
            RouterKind.PROVIDE_PENDING_VALUE,
        }
    if wanted == "waiting_task":
        return kind == RouterKind.WAITING_TASK
    if wanted == "resume_task":
        return kind == RouterKind.RESUME_TASK
    if wanted == "record_actual_minutes":
        return kind in {
            RouterKind.PROVIDE_PENDING_VALUE,
            RouterKind.CONTINUE_PENDING,
        }
    if wanted == "daily_plan":
        return kind in CONTROL_KINDS
    return False


def _awaiting_actual_minutes(pending: dict[str, Any]) -> bool:
    return pending.get("awaiting") == "actual_minutes" or pending.get(
        "pending_action"
    ) == "record_actual_minutes"
