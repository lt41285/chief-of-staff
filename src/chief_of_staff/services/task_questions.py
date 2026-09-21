"""Follow-up questions derived from missing fields (not from the model)."""

import re

from chief_of_staff.models.task import RequiredField, TaskDraft

_UKRAINIAN_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _is_ukrainian(text: str) -> bool:
    return _UKRAINIAN_RE.search(text) is not None


def _has_latin_letters(text: str) -> bool:
    return _LATIN_RE.search(text) is not None


def detect_reply_language(
    last_user_text: str,
    draft: TaskDraft | None = None,
    previous: str | None = None,
) -> str:
    """Ukrainian unless this turn or the draft is clearly English-only.

    Date-only / numeric replies keep the previous or draft language so
    «30.09.26» does not flip a Ukrainian intake to English.
    """
    if _is_ukrainian(last_user_text):
        return "uk"
    if _has_latin_letters(last_user_text) and not _is_ukrainian(last_user_text):
        return "en"
    samples = []
    if draft is not None:
        samples.extend(
            [
                draft.task_title,
                draft.desired_outcome,
                draft.project,
                *draft.people,
            ]
        )
    blob = " ".join(part for part in samples if part)
    if _is_ukrainian(blob):
        return "uk"
    if _has_latin_letters(blob) and not _is_ukrainian(blob):
        return "en"
    if previous in {"uk", "en"}:
        return previous
    return "uk"


def follow_up_questions(
    missing: tuple[RequiredField, ...],
    *,
    last_user_text: str,
    draft: TaskDraft | None = None,
    previous_language: str | None = None,
) -> list[str]:
    """Ask only for fields that are still missing. Title and outcome are separate."""
    pending = set(missing)
    uk = detect_reply_language(last_user_text, draft, previous_language) == "uk"
    questions: list[str] = []

    title_missing = RequiredField.TASK_TITLE in pending
    outcome_missing = RequiredField.DESIRED_OUTCOME in pending
    if title_missing and outcome_missing:
        questions.append(
            "Яке конкретне завдання (що зробити і з ким) і який результат вважається готовим?"
            if uk
            else "What exactly should be done (SMART title), and what does done look like?"
        )
    elif title_missing:
        questions.append(
            "Яке конкретне завдання (що зробити і з ким)?"
            if uk
            else "What exactly should be done (concrete action / SMART title)?"
        )
    elif outcome_missing:
        questions.append(
            "Який результат вважається готовим?"
            if uk
            else "What does done look like?"
        )
    pending.discard(RequiredField.TASK_TITLE)
    pending.discard(RequiredField.DESIRED_OUTCOME)

    if RequiredField.PROJECT in pending:
        questions.append("Для якого проєкту це?" if uk else "Which project is this for?")
        pending.discard(RequiredField.PROJECT)

    if RequiredField.DEADLINE in pending:
        questions.append("Який дедлайн (дата)?" if uk else "What is the deadline (date)?")
        pending.discard(RequiredField.DEADLINE)

    if RequiredField.ESTIMATED_MINUTES in pending:
        questions.append("Скільки хвилин займе?" if uk else "How many minutes will this take?")
        pending.discard(RequiredField.ESTIMATED_MINUTES)

    return questions


def format_follow_up(questions: list[str]) -> str:
    if len(questions) == 1:
        return questions[0]
    intro = "Потрібні ще кілька деталей:" if any(_is_ukrainian(q) for q in questions) else "I still need a few details:"
    lines = [intro]
    for index, question in enumerate(questions, start=1):
        lines.append(f"{index}. {question}")
    return "\n".join(lines)
