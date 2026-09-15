"""Rank existing tasks against a natural-language reference. No DB writes."""

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata

from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.services.names import normalize_entity_name, normalize_project_name

_STOP = frozenset(
    {
        "я",
        "вже",
        "як",
        "про",
        "для",
        "цей",
        "ця",
        "це",
        "той",
        "та",
        "і",
        "й",
        "з",
        "із",
        "зі",
        "the",
        "a",
        "an",
        "to",
        "for",
        "with",
        "about",
        "please",
        "mark",
        "as",
        "done",
        "complete",
        "completed",
        "task",
        "задачу",
        "задача",
        "завдання",
        "познач",
        "позначити",
        "виконано",
        "виконане",
        "виконану",
        "виконаним",
        "готово",
        "готову",
        "already",
        "call",
        "called",
        "talked",
        "spoke",
        "поговорив",
        "поговорила",
        "поговорили",
    }
)
_TOKEN = re.compile(r"[a-zа-яіїєґ0-9']+", re.IGNORECASE)


@dataclass(frozen=True)
class ScoredTask:
    task: PlanCandidate
    score: float


@dataclass(frozen=True)
class TaskMatch:
    selected: PlanCandidate | None
    candidates: tuple[PlanCandidate, ...]
    already_done: PlanCandidate | None


def match_task_reference(
    query: str,
    open_tasks: list[PlanCandidate],
    done_tasks: list[PlanCandidate] | None = None,
) -> TaskMatch:
    tokens = _tokens(query)
    open_scored = _rank(tokens, query, open_tasks)
    full = [item for item in open_scored if _token_coverage(tokens, item.task) >= 1.0]
    if len(full) > 1:
        exact = [
            item
            for item in full
            if _fold(item.task.title) == _fold(query) or _fold(query) == _fold(item.task.title)
        ]
        if len(exact) == 1:
            return TaskMatch(selected=exact[0].task, candidates=(), already_done=None)
        return TaskMatch(
            selected=None,
            candidates=tuple(item.task for item in full[:8]),
            already_done=None,
        )
    strong_open = [item for item in open_scored if item.score >= 0.45]
    if len(strong_open) == 1:
        return TaskMatch(selected=strong_open[0].task, candidates=(), already_done=None)
    if len(strong_open) > 1:
        top = strong_open[0].score
        close = [item for item in strong_open if top - item.score <= 0.12]
        if len(close) == 1:
            return TaskMatch(selected=close[0].task, candidates=(), already_done=None)
        return TaskMatch(
            selected=None,
            candidates=tuple(item.task for item in close[:8]),
            already_done=None,
        )
    done_scored = _rank(tokens, query, done_tasks or [])
    strong_done = [item for item in done_scored if item.score >= 0.55]
    if len(strong_done) == 1:
        return TaskMatch(selected=None, candidates=(), already_done=strong_done[0].task)
    return TaskMatch(selected=None, candidates=(), already_done=None)


def _token_coverage(tokens: list[str], task: PlanCandidate) -> float:
    if not tokens:
        return 0.0
    hay_tokens = _tokens(f"{task.title} {task.project} {' '.join(task.people)}")
    hits = sum(1 for token in tokens if _best_token_hit(token, hay_tokens) >= 0.7)
    return hits / len(tokens)


def _rank(tokens: list[str], query: str, tasks: list[PlanCandidate]) -> list[ScoredTask]:
    scored = [ScoredTask(task=task, score=_score(tokens, query, task)) for task in tasks]
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored


def _score(tokens: list[str], query: str, task: PlanCandidate) -> float:
    if not tokens:
        return 0.0
    title = _fold(task.title)
    hay_tokens = _tokens(
        f"{task.title} {task.project} {' '.join(task.people)}"
    )
    hits = sum(_best_token_hit(token, hay_tokens) for token in tokens)
    coverage = hits / len(tokens)
    seq = SequenceMatcher(None, _fold(query), title).ratio()
    person_bonus = 0.0
    for person in task.people:
        key = normalize_entity_name(person)
        if key and any(key in _fold(token) or _fold(token) in key for token in tokens):
            person_bonus = 0.2
            break
        if any(_prefix_hit(_fold(person), token) for token in tokens):
            person_bonus = 0.2
            break
    project_bonus = 0.0
    project_key = normalize_project_name(task.project)
    if project_key and any(project_key == normalize_project_name(token) for token in tokens):
        project_bonus = 0.15
    if title and _fold(query) in title:
        coverage = max(coverage, 0.9)
    return min(1.0, 0.7 * coverage + 0.2 * seq + person_bonus + project_bonus)


def _best_token_hit(token: str, hay_tokens: list[str]) -> float:
    best = 0.0
    for hay in hay_tokens:
        if token == hay:
            return 1.0
        if _prefix_hit(token, hay):
            best = max(best, 0.85)
        elif token in hay or hay in token:
            best = max(best, 0.7)
    return best


def _prefix_hit(left: str, right: str) -> bool:
    if min(len(left), len(right)) < 4:
        return False
    return left.startswith(right[:4]) or right.startswith(left[:4])


def _tokens(text: str) -> list[str]:
    folded = _fold(text)
    return [part for part in _TOKEN.findall(folded) if part not in _STOP and len(part) > 1]


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()
