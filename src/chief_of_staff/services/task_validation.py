"""Python-side task completeness rules. The model must not be the only gate."""

from datetime import date, datetime
import re

from chief_of_staff.models.task import RequiredField, TaskDraft, TaskExtractionSchema
from chief_of_staff.services.relative_deadline import resolve_deadline_expression

_VAGUE_TITLES = frozenset(
    {
        "task",
        "todo",
        "to do",
        "do it",
        "fix",
        "work",
        "meeting",
        "call",
        "задача",
        "завдання",
        "зробити",
        "робота",
        "зустріч",
        "дзвінок",
    }
)

_MINUTES_RE = re.compile(
    r"(?P<num>\d+)\s*(?:хв(?:илин(?:и|у)?)?|min(?:ute)?s?)?\b",
    re.IGNORECASE,
)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def _word_count(value: str) -> int:
    return len(re.findall(r"\S+", value))


def is_clear_smart_text(value: str | None, *, min_words: int = 3, min_chars: int = 12) -> bool:
    """Reject empty, tiny, or generic phrases that are not SMART-style titles."""
    cleaned = _clean_text(value)
    if cleaned is None:
        return False
    if cleaned.casefold() in _VAGUE_TITLES:
        return False
    if len(cleaned) < min_chars:
        return False
    return _word_count(cleaned) >= min_words


_OUTCOME_PREFIX = re.compile(
    r"^(?:результат|outcome|desired\s*outcome|done)\s*[-—–:]\s*",
    re.IGNORECASE,
)


def is_filled_outcome(value: str | None) -> bool:
    """Outcome is filled once it is a non-empty, non-generic result.

    Independent of task_title SMART rules (word-count). Two-word Ukrainian
    outcomes like 'погоджений бюджет' must count as present.
    """
    cleaned = _clean_text(value)
    if cleaned is None:
        return False
    if cleaned.casefold() in _VAGUE_TITLES:
        return False
    return len(cleaned) >= 2


def normalize_outcome_reply(user_text: str) -> str | None:
    cleaned = _clean_text(user_text)
    if cleaned is None:
        return None
    stripped = _OUTCOME_PREFIX.sub("", cleaned, count=1).strip().rstrip(".,;:!")
    return _clean_text(stripped) or cleaned


def parse_deadline(value: str | None, today: date | None = None) -> date | None:
    cleaned = _clean_text(value)
    if cleaned is None:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        pass
    if today is not None:
        return resolve_deadline_expression(cleaned, today)
    return None


def parse_estimated_minutes(value: str) -> int | None:
    match = _MINUTES_RE.search(value)
    if not match:
        return None
    minutes = int(match.group("num"))
    return minutes if minutes > 0 else None


def normalize_people(names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in names:
        cleaned = _clean_text(raw)
        if cleaned is None:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cleaned)
    return tuple(ordered)


def draft_from_extraction(schema: TaskExtractionSchema, *, today: date | None = None) -> TaskDraft:
    minutes = schema.estimated_minutes
    if minutes is not None and minutes <= 0:
        minutes = None
    return TaskDraft(
        task_title=_clean_text(schema.task_title),
        project=_clean_text(schema.project),
        people=normalize_people(schema.people),
        deadline=parse_deadline(schema.deadline, today),
        estimated_minutes=minutes,
        desired_outcome=_clean_text(schema.desired_outcome),
    )


def _keep_filled_text(base: str | None, incoming: str | None, *, smart: bool) -> str | None:
    if smart:
        if is_clear_smart_text(base):
            return _clean_text(base)
        if is_clear_smart_text(incoming):
            return _clean_text(incoming)
        return _clean_text(base) or _clean_text(incoming)
    if _clean_text(base) is not None:
        return _clean_text(base)
    return _clean_text(incoming)


def _keep_filled_outcome(base: str | None, incoming: str | None) -> str | None:
    if is_filled_outcome(base):
        return _clean_text(base)
    if is_filled_outcome(incoming):
        return _clean_text(incoming)
    return _clean_text(base) or _clean_text(incoming)


def _keep_filled[T](base: T | None, incoming: T | None) -> T | None:
    return base if base is not None else incoming


def merge_drafts(base: TaskDraft, incoming: TaskDraft, *, replace_filled: bool = False) -> TaskDraft:
    """Merge a patch into the existing draft.

    Stated (non-null) incoming fields replace the corresponding base fields.
    Null/empty incoming values never wipe known data.
    """
    del replace_filled
    people = base.people
    if incoming.people:
        people = normalize_people((*base.people, *incoming.people))
    return TaskDraft(
        task_title=_clean_text(incoming.task_title) or base.task_title,
        project=_clean_text(incoming.project) or base.project,
        people=people,
        deadline=incoming.deadline or base.deadline,
        importance=incoming.importance or base.importance,
        urgency=incoming.urgency or base.urgency,
        estimated_minutes=incoming.estimated_minutes or base.estimated_minutes,
        desired_outcome=_clean_text(incoming.desired_outcome) or base.desired_outcome,
    )


def _is_compound_intake_reply(text: str) -> bool:
    folded = text.casefold()
    markers = (
        "проєкт",
        "проект",
        "дедлайн",
        "deadline",
        "результат",
        "outcome",
        "хвилин",
        "годин",
        "півгодин",
        "пів годин",
    )
    hits = sum(1 for marker in markers if marker in folded)
    words = len(text.split())
    return hits >= 2 or (hits >= 1 and words > 10) or (text.count(".") >= 1 and words > 12)


def fill_missing_from_user_reply(
    draft: TaskDraft,
    user_text: str,
    missing_before: tuple[RequiredField, ...],
    *,
    today: date | None = None,
) -> TaskDraft:
    """Map a short single-field clarification onto still-missing fields.

    Compound utterances are ignored here — they are patched via extract_utterance_patch.
    """
    if _is_compound_intake_reply(user_text):
        return draft
    still_missing = set(missing_required_fields(draft))
    targets = {field for field in missing_before if field in still_missing}
    text = _clean_text(user_text)
    if text is None or not targets:
        return draft

    updates: dict[str, object] = {}
    deadline = parse_deadline(text, today)
    minutes = parse_estimated_minutes(text)
    folded = text.casefold()
    deadline_cued = deadline is not None or "дедлайн" in folded or "deadline" in folded
    from chief_of_staff.services.intake_project import strip_project_framing

    framed = strip_project_framing(text)
    has_project_frame = framed.casefold() != folded
    if RequiredField.DEADLINE in targets and deadline is not None:
        updates["deadline"] = deadline
    if RequiredField.ESTIMATED_MINUTES in targets and minutes is not None:
        updates["estimated_minutes"] = minutes
    if (
        RequiredField.PROJECT in targets
        and not deadline_cued
        and "результат" not in folded
        and "outcome" not in folded
        and len(text.split()) <= 8
        and (has_project_frame or RequiredField.DESIRED_OUTCOME not in targets)
    ):
        updates["project"] = framed
    elif RequiredField.DESIRED_OUTCOME in targets and not deadline_cued:
        outcome = normalize_outcome_reply(text)
        if is_filled_outcome(outcome):
            updates["desired_outcome"] = outcome
    elif RequiredField.TASK_TITLE in targets and is_clear_smart_text(text):
        updates["task_title"] = text
    if not updates:
        return draft
    return draft.model_copy(update=updates)


def missing_required_fields(draft: TaskDraft) -> tuple[RequiredField, ...]:
    missing: list[RequiredField] = []
    if not is_clear_smart_text(draft.task_title):
        missing.append(RequiredField.TASK_TITLE)
    if not is_filled_outcome(draft.desired_outcome):
        missing.append(RequiredField.DESIRED_OUTCOME)
    if _clean_text(draft.project) is None:
        missing.append(RequiredField.PROJECT)
    if draft.deadline is None:
        missing.append(RequiredField.DEADLINE)
    if draft.estimated_minutes is None:
        missing.append(RequiredField.ESTIMATED_MINUTES)
    return tuple(missing)


def is_ready(draft: TaskDraft) -> bool:
    return not missing_required_fields(draft)


def format_kyiv_clock(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M %A")
