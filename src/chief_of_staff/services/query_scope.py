"""Conversational query scope: history is context, not sticky filters."""

from __future__ import annotations

import re

from chief_of_staff.models.utterance_intent import QueryRelation, RouterKind, UtteranceInterpretation
from chief_of_staff.services.available_time import parse_available_time
from chief_of_staff.services.followup_intent import (
    followup_status_filter,
    looks_like_status_followup,
)

INHERIT_RELATIONS = {
    QueryRelation.CONTINUE_QUERY,
    QueryRelation.REFINE_QUERY,
    QueryRelation.CORRECT_QUERY,
}

PLAN_KIND_VALUES = frozenset({"fit_minutes", "plan"})

NEW_INTENT_KINDS = {
    RouterKind.LIST_PROJECTS,
    RouterKind.LIST_ARCHIVED_PROJECTS,
    RouterKind.ARCHIVE_PROJECT,
    RouterKind.RESTORE_PROJECT,
    RouterKind.DELETE_PROJECT_PERMANENTLY,
    RouterKind.CREATE_PROJECT,
    RouterKind.CREATE_TASK,
    RouterKind.COMPLETE_TASK,
    RouterKind.COMPLETE_STATEMENT,
    RouterKind.POSTPONE_TASK,
    RouterKind.WAITING_TASK,
    RouterKind.RESUME_TASK,
    RouterKind.PROVIDE_PENDING_VALUE,
    RouterKind.CANCEL_PENDING,
    RouterKind.CONTINUE_PENDING,
}

_DROP_PERSON = (
    "не тільки",
    "не лише",
    "не только",
    "без фільтра",
    "без фильтра",
    "покажи взагалі все",
    "покажи вообще все",
    "взагалі все",
)

_INFORMAL_CUES = (
    "говори до мене ти",
    "говорити до мене ти",
    "говорити до мене на ти",
    "говори до мене на ти",
    "можеш говорити до мене ти",
    "можеш до мене на ти",
    "можеш до мене ти",
    "на ти, а не ви",
    "ти, а не ви",
    "не кажи ви",
    "не кажіть ви",
    "не звертайся на ви",
    "давай на ти",
)

_PLANNING_CUES = (
    "встигн",
    "вільн",
    "за цей час",
    "що можу",
    "що можна",
    "вільну годин",
    "вільна годин",
    "вільну хвилин",
)


def looks_like_drop_person_filter(text: str) -> bool:
    folded = text.casefold()
    if "не тільки по ньому" in folded or "не лише по ньому" in folded:
        return True
    if "без фільтра по людині" in folded or "без фильтра по человеку" in folded:
        return True
    return any(cue in folded for cue in _DROP_PERSON)


def looks_like_informal_address(text: str) -> bool:
    folded = text.casefold()
    return any(cue in folded for cue in _INFORMAL_CUES)


def looks_like_standalone_planning(text: str) -> bool:
    folded = text.casefold()
    if not any(cue in folded for cue in _PLANNING_CUES):
        return False
    parsed = parse_available_time(text)
    if parsed.minutes is not None:
        return True
    return bool(re.search(r"годин|хвилин", folded))


def looks_like_global_overdue(text: str) -> bool:
    folded = " ".join(text.casefold().split())
    if "простроч" not in folded:
        return False
    if re.match(r"^а[\s,]", folded):
        return False
    return True


_NAME_SUFFIXES = (
    "ові",
    "ами",
    "ою",
    "ом",
    "ів",
    "ам",
    "ець",
    "цем",
    "цю",
    "ем",
    "ім",
    "а",
    "у",
    "і",
    "ї",
    "ю",
    "я",
    "е",
    "о",
    "и",
    "й",
    "ь",
)


def _name_stem(token: str) -> str:
    cleaned = re.sub(r"[^\w]+", "", token.casefold(), flags=re.UNICODE)
    if len(cleaned) >= 5 and cleaned.endswith(("ок", "ку", "ка")):
        return cleaned[:-2]
    for suffix in _NAME_SUFFIXES:
        keep = 4 if len(suffix) > 1 else 3
        if cleaned.endswith(suffix) and len(cleaned) - len(suffix) >= keep:
            return cleaned[: -len(suffix)]
    return cleaned


def utterance_mentions_name(text: str, name: str | None) -> bool:
    if not name or not name.strip():
        return False
    folded = text.casefold()
    token = " ".join(name.strip().casefold().split()).strip(" —–-")
    if not token:
        return False
    if token in folded:
        return True
    parts = token.split()
    if len(parts) > 1:
        return all(utterance_mentions_name(text, part) for part in parts)
    stem = _name_stem(token)
    if len(stem) < 3:
        return False
    words = re.findall(r"[^\W\d_]+", folded, flags=re.UNICODE)
    return any(_name_stem(word) == stem for word in words)


def clean_person_query(name: str | None) -> str | None:
    if name is None:
        return None
    cleaned = " ".join(name.split()).strip(" —–-\t")
    return cleaned or None


def person_stated_in_utterance(interp: UtteranceInterpretation, text: str) -> str | None:
    if looks_like_drop_person_filter(text):
        return None
    for name in (interp.replace_person, interp.person_query):
        cleaned = clean_person_query(name)
        if cleaned and utterance_mentions_name(text, cleaned):
            return cleaned
    return None


def resolve_query_relation(
    interp: UtteranceInterpretation,
    snapshot: ConversationSnapshot | None,
    text: str,
) -> QueryRelation:
    if interp.query_relation is not None:
        declared = interp.query_relation
    else:
        declared = None

    if looks_like_drop_person_filter(text):
        return QueryRelation.BROADEN_QUERY

    stated = person_stated_in_utterance(interp, text)
    snap_person = None
    if snapshot is not None:
        snap_person = clean_person_query(snapshot.person_query) or snapshot.person_name

    if looks_like_standalone_planning(text):
        mentioned = bool(stated) or utterance_mentions_name(text, snap_person)
        if not mentioned:
            return QueryRelation.NEW_INTENT

    if interp.kind in {RouterKind.SUM_ESTIMATES, RouterKind.INSPECT_LISTED}:
        return QueryRelation.CONTINUE_QUERY

    if looks_like_refine_overdue(text) and snap_person:
        return QueryRelation.REFINE_QUERY

    if looks_like_status_followup(text) and snap_person:
        return QueryRelation.REFINE_QUERY

    if looks_like_global_overdue(text) and not interp.inherit_context:
        if interp.kind not in {RouterKind.REFINE_PREVIOUS, RouterKind.RETRY_PREVIOUS}:
            if not interp.continue_previous and not interp.retry_previous:
                if declared not in INHERIT_RELATIONS:
                    return QueryRelation.NEW_QUERY

    if declared is not None:
        return declared

    if interp.replace_person or interp.kind == RouterKind.CORRECT_ENTITY:
        return QueryRelation.CORRECT_QUERY
    if interp.kind == RouterKind.RETRY_PREVIOUS or interp.retry_previous or interp.continue_previous:
        return QueryRelation.CONTINUE_QUERY
    if interp.kind == RouterKind.REFINE_PREVIOUS or interp.inherit_context:
        return QueryRelation.REFINE_QUERY
    if interp.kind == RouterKind.GENERAL_CHAT:
        return QueryRelation.GENERAL_CHAT
    if interp.kind in NEW_INTENT_KINDS:
        return QueryRelation.NEW_INTENT
    if interp.kind == RouterKind.FIT_MINUTES:
        if stated:
            return QueryRelation.NEW_QUERY
        return QueryRelation.NEW_INTENT
    if interp.kind in {RouterKind.LIST_ALL_TASKS, RouterKind.LIST_PERIOD}:
        return QueryRelation.NEW_QUERY
    if interp.kind == RouterKind.PEOPLE_TASKS_QUERY:
        return QueryRelation.NEW_QUERY
    return QueryRelation.NEW_QUERY


def apply_query_scope(
    interp: UtteranceInterpretation,
    snapshot: ConversationSnapshot | None,
    text: str,
) -> UtteranceInterpretation:
    relation = resolve_query_relation(interp, snapshot, text)
    updates: dict[str, object] = {"query_relation": relation}
    stated = person_stated_in_utterance(interp, text)

    if interp.kind in NEW_INTENT_KINDS or interp.kind == RouterKind.GENERAL_CHAT:
        updates["inherit_context"] = False
        if interp.kind != RouterKind.CREATE_TASK and not stated:
            if not interp.person_query or not utterance_mentions_name(text, interp.person_query):
                updates["person_query"] = stated
        return interp.model_copy(update=updates)

    if relation == QueryRelation.BROADEN_QUERY:
        minutes = interp.available_minutes
        last_kind = snapshot.last_query_kind if snapshot else None
        last_minutes = snapshot.last_available_minutes if snapshot else None
        updates.update(
            {
                "person_query": None,
                "replace_person": None,
                "inherit_context": False,
                "clear_person_filter": True,
                "project_query": None,
            }
        )
        if last_kind in PLAN_KIND_VALUES:
            updates["kind"] = RouterKind.FIT_MINUTES
            updates["available_minutes"] = minutes or last_minutes
        else:
            updates["kind"] = RouterKind.LIST_ALL_TASKS
            updates["include_overdue"] = interp.include_overdue
        return interp.model_copy(update=updates)

    if relation == QueryRelation.CORRECT_QUERY:
        name = interp.replace_person or stated or interp.person_query
        updates.update(
            {
                "kind": RouterKind.PEOPLE_TASKS_QUERY,
                "person_query": name,
                "replace_person": name,
                "inherit_context": False,
            }
        )
        if snapshot is not None and not interp.project_query:
            updates["project_query"] = snapshot.project_name
        if snapshot is not None and interp.status_filter is None:
            updates["status_filter"] = snapshot.status_filter
        return interp.model_copy(update=updates)

    if relation == QueryRelation.CONTINUE_QUERY:
        if interp.kind in {RouterKind.SUM_ESTIMATES, RouterKind.INSPECT_LISTED}:
            updates["inherit_context"] = True
            updates["use_listed_ids"] = True
            return interp.model_copy(update=updates)
        updates.update(
            {
                "kind": RouterKind.PEOPLE_TASKS_QUERY,
                "broader_search": True,
                "retry_previous": True,
                "inherit_context": True,
            }
        )
        if snapshot is not None:
            if not stated:
                updates["person_query"] = snapshot.person_query or snapshot.person_name
            else:
                updates["person_query"] = stated
            if not interp.project_query:
                updates["project_query"] = snapshot.project_name
            if interp.status_filter is None:
                updates["status_filter"] = snapshot.status_filter
        return interp.model_copy(update=updates)

    if relation == QueryRelation.REFINE_QUERY:
        last_kind = snapshot.last_query_kind if snapshot else None
        listed_only = bool(interp.exclude_indexes or interp.use_listed_ids)
        period_refine = last_kind == "list_period" or (
            snapshot is not None
            and snapshot.last_period
            and not snapshot.person_query
            and not snapshot.person_name
        )
        if listed_only:
            updates["use_listed_ids"] = True
            updates["kind"] = RouterKind.REFINE_PREVIOUS
            if snapshot is not None and not stated:
                updates["person_query"] = snapshot.person_query or snapshot.person_name
            return interp.model_copy(update=updates)
        if period_refine and not stated:
            updates["kind"] = RouterKind.LIST_PERIOD
            updates["period"] = interp.period or (snapshot.last_period if snapshot else None)
            updates["person_query"] = None
            if interp.exclude_overdue:
                updates["exclude_overdue"] = True
            return interp.model_copy(update=updates)
        person = stated
        if person is None and snapshot is not None:
            person = clean_person_query(snapshot.person_query) or snapshot.person_name
        updates["person_query"] = clean_person_query(person)
        updates["inherit_context"] = True
        if looks_like_status_followup(text):
            updates["status_filter"] = followup_status_filter(text)
        elif interp.status_filter is not None:
            updates["status_filter"] = interp.status_filter
        elif snapshot is not None:
            updates["status_filter"] = snapshot.status_filter
        if interp.period:
            updates["period"] = interp.period
        elif snapshot is not None and snapshot.last_period:
            updates["period"] = snapshot.last_period
        if interp.include_overdue or looks_like_refine_overdue(text):
            updates["include_overdue"] = True
        if person:
            updates["kind"] = RouterKind.PEOPLE_TASKS_QUERY
        elif updates.get("include_overdue"):
            updates["kind"] = RouterKind.LIST_ALL_TASKS
        return interp.model_copy(update=updates)

    # new_query / new_intent / conservative default: no snapshot filters
    updates["inherit_context"] = False
    if relation == QueryRelation.NEW_INTENT and looks_like_standalone_planning(text):
        minutes = interp.available_minutes or parse_available_time(text).minutes
        if minutes is None and re.search(r"годин", text.casefold()):
            minutes = 60
        if minutes is None and re.search(r"хвилин", text.casefold()):
            minutes = 30
        updates["available_minutes"] = minutes
        if stated:
            updates["person_query"] = stated
            updates["kind"] = RouterKind.PEOPLE_TASKS_QUERY
        else:
            updates["person_query"] = None
            updates["kind"] = RouterKind.FIT_MINUTES
            updates["clear_person_filter"] = True
        return interp.model_copy(update=updates)

    if looks_like_global_overdue(text) and not stated:
        updates["kind"] = RouterKind.LIST_ALL_TASKS
        updates["include_overdue"] = True
        updates["person_query"] = None
        updates["clear_person_filter"] = True
        return interp.model_copy(update=updates)

    if stated:
        updates["person_query"] = stated
    elif interp.person_query and not utterance_mentions_name(text, interp.person_query):
        updates["person_query"] = None
        updates["clear_person_filter"] = True
    return interp.model_copy(update=updates)


def looks_like_refine_overdue(text: str) -> bool:
    folded = " ".join(text.casefold().split())
    return bool(re.match(r"^а[\s,]", folded) and "простроч" in folded)


def should_reset_query_scope(interp: UtteranceInterpretation) -> bool:
    relation = interp.query_relation
    if relation in {
        QueryRelation.NEW_QUERY,
        QueryRelation.NEW_INTENT,
        QueryRelation.BROADEN_QUERY,
        QueryRelation.GENERAL_CHAT,
    }:
        return True
    if interp.clear_person_filter:
        return True
    if interp.kind in NEW_INTENT_KINDS:
        return True
    return False
