"""Detect completed-action statements and rank matching open tasks.

Business rules live here: phrasing gates, amount normalization, and
confidence thresholds. Confirmation still happens in the lifecycle service.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field

from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.services.names import normalize_entity_name, normalize_project_name

STRONG_SCORE = 0.55
PLAUSIBLE_SCORE = 0.42
CLOSE_DELTA = 0.12
MAX_PICK = 3
UNMATCHED_INTAKE_RATIO = 0.4

_APOS = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})
_TOKEN = re.compile(r"[a-zа-яіїєґ0-9']+", re.IGNORECASE)

_STOP = frozenset(
    {
        "я",
        "ми",
        "ти",
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
        "їй",
        "йому",
        "її",
        "the",
        "a",
        "an",
        "to",
        "for",
        "with",
        "about",
        "from",
        "her",
        "him",
        "this",
        "that",
        "and",
        "of",
        "in",
        "on",
        "i",
        "we",
        "my",
        "our",
        "все",
        "уже",
        "що",
        "чи",
        "а",
        "але",
        "же",
    }
)

_PROSPECTIVE = re.compile(
    r"(?i)(?:^|\s)(?:"
    r"треба|потрібно|потрібн[аое]|нагадай(?:те)?|нагадайте|"
    r"нагадування|don't\s+forget|do\s+not\s+forget|"
    r"remind(?:\s+me)?|need(?:s)?\s+to|have\s+to|has\s+to|"
    r"must|should|let'?s|давай"
    r")\b"
)

_INFINITIVE_START = re.compile(
    r"(?i)^\s*(?:"
    r"домовитися|поговорити|передати|зробити|підписати|"
    r"оплатити|купити|надіслати|відправити|зустрітися|"
    r"отримати|вирішити|завершити|погодити|"
    r"to\s+(?:agree|speak|talk|send|pay|meet|deliver|buy|sign|finish|complete)"
    r")\b"
)

_PAST_UA = re.compile(
    r"(?i)\b(?:"
    r"викона(?:в|ла|ли)|"
    r"зробив(?:ла|ли)?|зробила|"
    r"домови(?:вся|лась|лася|лись|лися)|"
    r"передав(?:ла|ли)?|передано|"
    r"відправив(?:ла|ли)?|"
    r"поговорив(?:ла|ли)?|"
    r"погодив(?:ла|ли)?|погодили|"
    r"підписав(?:ла|ли)?|"
    r"завершив(?:ла|ли)?|"
    r"оплатив(?:ла|ли)?|"
    r"купив(?:ла|ли)?|"
    r"надіслав(?:ла|ли)?|"
    r"зустрів(?:ся|лась|лася)|зустріли|"
    r"отримав(?:ла|ли)?|"
    r"вирішив(?:ла|ли)?"
    r")\b"
)

_PAST_EN = re.compile(
    r"(?i)\b(?:i\s+|we\s+)?(?:"
    r"did|completed|finished|spoke\s+with|talked\s+(?:to|with)|"
    r"sent|paid|agreed\s+with|met\s+with|delivered|bought|signed"
    r")\b"
)

_SHORT = re.compile(
    r"(?i)^\s*(?:"
    r"зробив(?:ла)?|готово|виконано|домовився|домовилась|домовилася|"
    r"done|finished|completed"
    r")\s*[.!]?\s*$"
)

_TIS = re.compile(
    r"(?i)(\d+(?:[ \u00a0]\d{3})*)\s*(?:тис(?:яч\w*)?|[kк])\b"
)
_GROUPED = re.compile(r"\b(\d{1,3}(?:[ \u00a0]\d{3}){1,3})\b")
_PLAIN = re.compile(r"\b(\d{4,9})\b")

_SUFFIXES = (
    "итися",
    "итись",
    "тися",
    "тись",
    "ться",
    "ився",
    "лась",
    "лася",
    "лись",
    "лися",
    "вся",
    "ення",
    "ання",
    "ування",
    "ацію",
    "ації",
    "ація",
    "ати",
    "ити",
    "іти",
    "ути",
    "ти",
    "ала",
    "ила",
    "іла",
    "ула",
    "али",
    "или",
    "ав",
    "ив",
    "ув",
    "ів",
    "ею",
    "ою",
    "єю",
    "ам",
    "ах",
    "ую",
)

_ACTION_STEMS = frozenset(
    {
        "домов",
        "перед",
        "передач",
        "зроб",
        "поговор",
        "говор",
        "погод",
        "підпис",
        "подпис",
        "заверш",
        "оплат",
        "куп",
        "надісл",
        "відправ",
        "зустр",
        "отрим",
        "виріш",
        "spoke",
        "talk",
        "sent",
        "paid",
        "agre",
        "met",
        "deliver",
        "bought",
        "sign",
        "did",
        "finish",
        "complet",
    }
)

_NEW_TASK_OVERRIDE = re.compile(
    r"(?i)^\s*(?:це\s+нова\s+задача|нова\s+задача|"
    r"this\s+is\s+a\s+new\s+task|new\s+task)\s*[.!]?\s*$"
)


class CompletionPhrasing(StrEnum):
    COMPLETED = "completed"
    PROSPECTIVE = "prospective"
    SHORT = "short"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CompletionMatch:
    selected: PlanCandidate | None
    candidates: tuple[PlanCandidate, ...]
    scores: tuple[float, ...] = ()


class CompletionActionLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_completed_past_action: bool = Field(
        description="True if the user reports an action already done"
    )
    is_prospective_or_imperative: bool = Field(
        description="True if this is a new to-do, reminder, or command to do later"
    )


COMPLETION_ACTION_INSTRUCTIONS = """
Classify whether a Ukrainian or English message reports a COMPLETED past action
versus a new task / reminder / imperative.

is_completed_past_action: the speaker already did something
("я домовився", "передав кошти", "I sent", "все погодили").
is_prospective_or_imperative: they still need to do it
("треба домовитися", "домовитися з Лесею", "нагадайте поговорити",
"need to pay", infinitive task titles).

If both could apply, prefer prospective.
Short "готово"/"зробив"/"done" is a completed-action signal, not a new task.
""".strip()


def classify_completion_phrasing(text: str) -> CompletionPhrasing:
    raw = " ".join(text.translate(_APOS).split()).strip()
    if not raw:
        return CompletionPhrasing.UNKNOWN
    if _SHORT.fullmatch(raw):
        return CompletionPhrasing.SHORT
    if _PROSPECTIVE.search(raw):
        return CompletionPhrasing.PROSPECTIVE
    past = bool(_PAST_UA.search(raw) or _PAST_EN.search(raw))
    if _INFINITIVE_START.match(raw) and not past:
        return CompletionPhrasing.PROSPECTIVE
    if past:
        return CompletionPhrasing.COMPLETED
    return CompletionPhrasing.UNKNOWN


def looks_like_completed_statement(text: str) -> bool:
    return classify_completion_phrasing(text) in {
        CompletionPhrasing.COMPLETED,
        CompletionPhrasing.SHORT,
    }


def content_token_count(text: str) -> int:
    return len(_tokens(text))


def is_new_task_override(text: str) -> bool:
    return bool(_NEW_TASK_OVERRIDE.fullmatch(" ".join(text.split()).strip()))


def extract_amounts(text: str) -> frozenset[int]:
    folded = _fold(text).replace("\u00a0", " ")
    amounts: set[int] = set()
    used: list[tuple[int, int]] = []

    def _taken(span: tuple[int, int]) -> bool:
        start, end = span
        return any(start < other_end and end > other_start for other_start, other_end in used)

    for match in _TIS.finditer(folded):
        number = int(re.sub(r"\s+", "", match.group(1)))
        amounts.add(number * 1000)
        used.append(match.span())
    for match in _GROUPED.finditer(folded):
        if _taken(match.span()):
            continue
        amounts.add(int(re.sub(r"[\s\u00a0]+", "", match.group(1))))
        used.append(match.span())
    for match in _PLAIN.finditer(folded):
        if _taken(match.span()):
            continue
        amounts.add(int(match.group(1)))
        used.append(match.span())
    return frozenset(amounts)


def match_completion_statement(
    utterance: str, open_tasks: list[PlanCandidate]
) -> CompletionMatch:
    if not open_tasks:
        return CompletionMatch(selected=None, candidates=())
    utterance_tokens = _tokens(utterance)
    utterance_amounts = extract_amounts(utterance)
    ranked = sorted(
        (
            (task, _score(utterance, utterance_tokens, utterance_amounts, task))
            for task in open_tasks
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    strong = [
        (task, score)
        for task, score in ranked
        if score >= STRONG_SCORE and _credible(utterance, utterance_tokens, utterance_amounts, task)
    ]
    if len(strong) == 1:
        return CompletionMatch(selected=strong[0][0], candidates=(), scores=(strong[0][1],))
    if len(strong) > 1:
        top = strong[0][1]
        close = [item for item in strong if top - item[1] <= CLOSE_DELTA]
        if len(close) == 1:
            return CompletionMatch(selected=close[0][0], candidates=(), scores=(close[0][1],))
        return CompletionMatch(
            selected=None,
            candidates=tuple(task for task, _ in close[:MAX_PICK]),
            scores=tuple(score for _, score in close[:MAX_PICK]),
        )
    plausible = [
        (task, score)
        for task, score in ranked
        if score >= PLAUSIBLE_SCORE
        and _credible(utterance, utterance_tokens, utterance_amounts, task)
    ]
    if len(plausible) == 1 and _unmatched_ratio(utterance_tokens, plausible[0][0]) <= UNMATCHED_INTAKE_RATIO:
        return CompletionMatch(selected=plausible[0][0], candidates=(), scores=(plausible[0][1],))
    if 2 <= len(plausible) <= MAX_PICK:
        return CompletionMatch(
            selected=None,
            candidates=tuple(task for task, _ in plausible),
            scores=tuple(score for _, score in plausible),
        )
    if len(plausible) > MAX_PICK:
        return CompletionMatch(
            selected=None,
            candidates=tuple(task for task, _ in plausible[:MAX_PICK]),
            scores=tuple(score for _, score in plausible[:MAX_PICK]),
        )
    return CompletionMatch(selected=None, candidates=())


def _credible(
    utterance: str,
    tokens: list[str],
    amounts: frozenset[int],
    task: PlanCandidate,
) -> bool:
    unmatched = _unmatched_ratio(tokens, task)
    amount_hit = _amount_hit(amounts, task)
    if unmatched > UNMATCHED_INTAKE_RATIO and not amount_hit:
        return False
    evidence = 0
    if _person_hit(tokens, task):
        evidence += 1
    if amount_hit:
        evidence += 1
    if _action_hit(tokens, task):
        evidence += 1
    if _project_hit(tokens, task):
        evidence += 1
    coverage = _token_coverage(tokens, task)
    return evidence >= 2 or coverage >= 0.75 or amount_hit


def _score(
    utterance: str,
    tokens: list[str],
    amounts: frozenset[int],
    task: PlanCandidate,
) -> float:
    if not tokens:
        return 0.0
    hay_tokens = _task_tokens(task)
    hits = sum(_best_token_hit(token, hay_tokens) for token in tokens)
    coverage = hits / len(tokens)
    seq = SequenceMatcher(None, _fold(utterance), _fold(task.title)).ratio()
    score = 0.55 * coverage + 0.15 * seq
    if _person_hit(tokens, task):
        score += 0.18
    if _amount_hit(amounts, task):
        score += 0.22
    if _action_hit(tokens, task):
        score += 0.12
    if _project_hit(tokens, task):
        score += 0.1
    score -= 0.2 * _unmatched_ratio(tokens, task)
    return min(1.0, max(0.0, score))


def _amount_hit(amounts: frozenset[int], task: PlanCandidate) -> bool:
    if not amounts:
        return False
    return bool(amounts & extract_amounts(_task_text(task)))


def _person_hit(tokens: list[str], task: PlanCandidate) -> bool:
    token_lemmas = {_lemma(token) for token in tokens}
    for person in task.people:
        for part in person.split():
            key = normalize_entity_name(part)
            lemma = _lemma(key)
            if not lemma:
                continue
            if any(
                lemma == token or token.startswith(lemma[:4]) or lemma.startswith(token[:4])
                for token in token_lemmas
                if len(token) >= 3 and len(lemma) >= 3
            ):
                return True
            if any(key in token or token in key for token in tokens if len(token) >= 3):
                return True
    return False


def _project_hit(tokens: list[str], task: PlanCandidate) -> bool:
    project_key = normalize_project_name(task.project)
    if not project_key:
        return False
    return any(project_key == normalize_project_name(token) for token in tokens)


def _action_hit(tokens: list[str], task: PlanCandidate) -> bool:
    utterance_stems = {_action_stem(_lemma(token)) for token in tokens} & _ACTION_STEMS
    task_stems = {_action_stem(_lemma(token)) for token in _task_tokens(task)} & _ACTION_STEMS
    return bool(utterance_stems & task_stems)


def _action_stem(lemma: str) -> str:
    for stem in sorted(_ACTION_STEMS, key=len, reverse=True):
        if lemma.startswith(stem):
            return stem
    return lemma[:5]


def _unmatched_ratio(tokens: list[str], task: PlanCandidate) -> float:
    if not tokens:
        return 0.0
    hay = _task_tokens(task)
    missed = sum(1 for token in tokens if _best_token_hit(token, hay) < 0.7)
    return missed / len(tokens)


def _token_coverage(tokens: list[str], task: PlanCandidate) -> float:
    if not tokens:
        return 0.0
    hay = _task_tokens(task)
    hits = sum(1 for token in tokens if _best_token_hit(token, hay) >= 0.7)
    return hits / len(tokens)


def _best_token_hit(token: str, hay_tokens: list[str]) -> float:
    lemma = _lemma(token)
    best = 0.0
    for hay in hay_tokens:
        hay_lemma = _lemma(hay)
        if token == hay or lemma == hay_lemma:
            return 1.0
        if _prefix_hit(lemma, hay_lemma) or _prefix_hit(token, hay):
            best = max(best, 0.85)
        elif lemma in hay_lemma or hay_lemma in lemma:
            best = max(best, 0.7)
    return best


def _prefix_hit(left: str, right: str) -> bool:
    if min(len(left), len(right)) < 4:
        return False
    return left.startswith(right[:4]) or right.startswith(left[:4])


def _task_text(task: PlanCandidate) -> str:
    return " ".join(
        part
        for part in (task.title, task.desired_outcome, task.project, " ".join(task.people))
        if part
    )


def _task_tokens(task: PlanCandidate) -> list[str]:
    return _tokens(_task_text(task))


def _tokens(text: str) -> list[str]:
    folded = _fold(text)
    return [part for part in _TOKEN.findall(folded) if part not in _STOP and len(part) > 1]


def _lemma(token: str) -> str:
    if token.isdigit():
        return token
    text = token
    changed = True
    while changed and len(text) > 4:
        changed = False
        for suffix in _SUFFIXES:
            if text.endswith(suffix) and len(text) - len(suffix) >= 3:
                text = text[: -len(suffix)]
                changed = True
                break
    if len(text) > 3:
        for suffix in ("у", "і", "и", "а", "я", "ю", "е", "о"):
            if text.endswith(suffix) and len(text) - 1 >= 3:
                return text[:-1]
    return text


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()
