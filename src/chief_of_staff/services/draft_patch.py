"""Patch the pending task draft from a new utterance. Python owns the merge."""

from __future__ import annotations

from datetime import date
from difflib import SequenceMatcher
import re

from chief_of_staff.models.task import TaskDraft
from chief_of_staff.services.available_time import parse_available_time
from chief_of_staff.services.relative_deadline import resolve_deadline_expression

_PROJECT_SPAN = re.compile(
    r"(?i)(?:проєкту|проекту|проєкт|проект)\s+"
    r"(?P<name>.+?)"
    r"(?=\s*(?:[.!?;]|дедлайн|deadline|результат|outcome|пів\s*годин|хвилин|$))"
)
_OUTCOME_SPAN = re.compile(
    r"(?i)(?:результат(?:\s+є)?|outcome|desired\s*outcome)\s*[:\-—]?\s*"
    r"(?P<body>.+?)"
    r"(?=\s*(?:[.!?;]\s*)?(?:дедлайн|deadline|проєкт|проект|$))"
)
_NOT_BUT = re.compile(
    r"(?i)не\s+(?P<wrong>[\w'’-]{3,})\s*,?\s*а\s+(?P<right>[\w'’-]{3,})"
)
_METADATA_SENTENCE = re.compile(
    r"(?i)(?:^|[.!?]\s+)(?:проєкт|проект)\b.+$"
)
_DURATION = re.compile(
    r"(?i)\b(?:пів\s*годин[уи]|півтори\s*годин[уи]?|\d+(?:[.,]\d+)?\s*"
    r"(?:хвилин(?:и|у)?|хв\.?|годин[уи]?|год\.?|minutes?|hours?))\b"
)
_DURING = re.compile(r"(?i)\bв\s+часі\b")
_COMPOUND_MARKERS = (
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
_OUTCOME_PREFIX = re.compile(
    r"^(?:результат|outcome|desired\s*outcome|done)\s*[-—–:]\s*",
    re.IGNORECASE,
)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def _is_filled_outcome(value: str | None) -> bool:
    cleaned = _clean_text(value)
    if cleaned is None:
        return False
    return len(cleaned) >= 2


def _normalize_outcome(user_text: str) -> str | None:
    cleaned = _clean_text(user_text)
    if cleaned is None:
        return None
    stripped = _OUTCOME_PREFIX.sub("", cleaned, count=1).strip().rstrip(".,;:!")
    cleaned = _clean_text(stripped) or cleaned
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _normalize_people(names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
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


def is_compound_intake_reply(text: str) -> bool:
    folded = text.casefold()
    hits = sum(1 for marker in _COMPOUND_MARKERS if marker in folded)
    words = len(text.split())
    return hits >= 2 or (hits >= 1 and words > 10) or (text.count(".") >= 1 and words > 12)


def utterance_mentions_project(text: str) -> bool:
    folded = " ".join(text.casefold().split())
    if re.search(r"(?i)(?:проєкт|проект)\b", folded):
        return True
    if re.search(r"(?i)номер(?:ом)?|під\s+номером|четверт|#|№", folded):
        return True
    if re.search(r"(?i)\b(?:цей\s+проєкт|цей\s+проект|так,?\s+він)\b", folded):
        return True
    return False


def looks_like_confirm_candidate(text: str) -> bool:
    folded = " ".join(text.casefold().split())
    return folded in {
        "так",
        "yes",
        "y",
        "так він",
        "так, він",
        "цей",
        "цей проєкт",
        "цей проект",
        "він",
        "воно",
        "саме він",
        "саме цей",
    }


def extract_utterance_patch(text: str, today: date) -> TaskDraft:
    project = None
    match = _PROJECT_SPAN.search(text)
    if match:
        project = _clean_text(match.group("name"))
        if project:
            project = _DURATION.sub("", project)
            project = project.strip(" .,:;")
    outcome = None
    out = _OUTCOME_SPAN.search(text)
    if out:
        outcome = _normalize_outcome(out.group("body"))
    minutes = parse_available_time(text).minutes
    deadline = resolve_deadline_expression(text, today)
    return TaskDraft(
        project=project,
        desired_outcome=outcome if _is_filled_outcome(outcome) else None,
        estimated_minutes=minutes,
        deadline=deadline,
    )


def apply_draft_patch(base: TaskDraft, incoming: TaskDraft) -> TaskDraft:
    """Replace only fields the incoming patch actually states. Nulls never wipe."""
    people = base.people
    if incoming.people:
        people = _normalize_people((*base.people, *incoming.people))
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


def sanitize_ai_patch(base: TaskDraft, incoming: TaskDraft, utterance: str) -> TaskDraft:
    """Drop AI fields that would contaminate an already isolated draft."""
    project = incoming.project
    if project:
        if _same_phrase(project, incoming.desired_outcome) or _same_phrase(
            project, base.desired_outcome
        ):
            project = None
        elif base.project and not utterance_mentions_project(utterance):
            project = None
    title = incoming.task_title
    if title and base.task_title:
        if _same_phrase(title, incoming.desired_outcome) or _same_phrase(
            title, incoming.project
        ):
            title = None
        elif SequenceMatcher(None, title.casefold(), base.task_title.casefold()).ratio() < 0.55:
            if utterance_mentions_project(utterance) or _OUTCOME_SPAN.search(utterance):
                title = None
    return incoming.model_copy(update={"project": project, "task_title": title})


def clean_task_title(title: str | None, *, project: str | None = None) -> str | None:
    text = _clean_text(title)
    if text is None:
        return None
    text = _METADATA_SENTENCE.sub("", text).strip(" .")
    text = _DURATION.sub("", text)
    if project:
        folded_project = project.casefold()
        parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
        kept = [part for part in parts if folded_project not in part.casefold()]
        if kept:
            text = " ".join(kept)
    text = re.sub(r"\s{2,}", " ", text).strip(" .,;:")
    return _clean_text(text) or _clean_text(title)


def apply_title_corrections(title: str | None, utterance: str) -> str | None:
    current = _clean_text(title)
    if current is None:
        return None
    updated = current
    for match in _NOT_BUT.finditer(utterance):
        wrong = match.group("wrong")
        right = match.group("right")
        updated = re.sub(re.escape(wrong), right, updated, flags=re.IGNORECASE)
    if re.search(r"(?i)під\s+час", utterance) and _DURING.search(updated):
        updated = _DURING.sub("під час", updated)
    updated = _replace_similar_tokens(updated, re.findall(r"[^\W\d_]+", utterance, flags=re.UNICODE))
    return _clean_text(updated) or current


def _replace_similar_tokens(title: str, utter_tokens: list[str]) -> str:
    parts = title.split()
    present = {token.casefold() for token in utter_tokens}
    used: set[int] = set()
    for index, token in enumerate(parts):
        stripped = token.strip(".,;:!?")
        if len(stripped) < 3:
            continue
        if stripped.casefold() in present:
            continue
        best_idx = None
        best_ratio = 0.0
        for u_index, other in enumerate(utter_tokens):
            if u_index in used or len(other) < 3:
                continue
            a, b = stripped.casefold(), other.casefold()
            if a == b or a in b or b in a:
                continue
            if abs(len(a) - len(b)) > 1:
                continue
            dist = _edit_distance(a, b)
            ratio = SequenceMatcher(None, a, b).ratio()
            if min(len(a), len(b)) <= 4:
                if dist > 1:
                    continue
                threshold = 0.66
            else:
                if dist <= 2:
                    ratio = max(ratio, 0.85)
                threshold = 0.8
            if dist > 2:
                continue
            if ratio >= threshold and ratio > best_ratio:
                best_ratio = ratio
                best_idx = u_index
        if best_idx is not None:
            parts[index] = _keep_token_punct(token, utter_tokens[best_idx])
            used.add(best_idx)
    return " ".join(parts)


def _keep_token_punct(original: str, replacement: str) -> str:
    suffix = original[len(original.rstrip(".,;:!? ")) :]
    return replacement + suffix


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, lch in enumerate(left, start=1):
        current = [i]
        for j, rch in enumerate(right, start=1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            replace = previous[j - 1] + (lch != rch)
            current.append(min(insert, delete, replace))
        previous = current
    return previous[-1]


def looks_like_project_only_reply(text: str) -> bool:
    compact = " ".join(text.split())
    if is_compound_intake_reply(compact):
        return False
    folded = compact.casefold()
    if "дедлайн" in folded or "deadline" in folded or "результат" in folded:
        return False
    if parse_available_time(compact).minutes is not None:
        return False
    if len(compact) > 80 or len(compact.split()) > 8:
        return False
    return True


def _same_phrase(left: str | None, right: str | None) -> bool:
    a = _clean_text(left)
    b = _clean_text(right)
    if a is None or b is None:
        return False
    return a.casefold() == b.casefold()
