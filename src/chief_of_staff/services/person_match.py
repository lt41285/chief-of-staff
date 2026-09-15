"""Match query names to stored people, including Ukrainian inflections."""

from dataclasses import dataclass
from difflib import SequenceMatcher
from uuid import UUID

from chief_of_staff.services.names import normalize_entity_name

_SUFFIXES = (
    "ові",
    "еві",
    "ою",
    "ею",
    "ами",
    "ями",
    "ах",
    "ях",
    "ом",
    "ем",
    "ів",
    "ові",
    "а",
    "у",
    "і",
    "и",
    "е",
    "ю",
    "я",
    "о",
    "й",
)


@dataclass(frozen=True)
class PersonHit:
    name: str
    person_id: UUID | None = None
    score: float = 0.0


@dataclass(frozen=True)
class PersonMatch:
    selected: PersonHit | None
    candidates: tuple[PersonHit, ...] = ()


def resolve_person(query: str, people: list[PersonHit]) -> PersonMatch:
    cleaned = " ".join(query.split()).strip(" .,!?:;«»\"'")
    if not cleaned or not people:
        return PersonMatch(selected=None)
    ranked = sorted(
        (PersonHit(name=person.name, person_id=person.person_id, score=_score(cleaned, person.name))
         for person in people),
        key=lambda item: item.score,
        reverse=True,
    )
    strong = [item for item in ranked if item.score >= 0.86]
    if len(strong) == 1:
        return PersonMatch(selected=strong[0])
    if len(strong) > 1:
        top = strong[0].score
        close = tuple(item for item in strong if top - item.score <= 0.08)
        if len(close) == 1:
            return PersonMatch(selected=close[0])
        return PersonMatch(selected=None, candidates=close[:8])
    unique_partial = _unique_partial(cleaned, ranked)
    if unique_partial is not None:
        return unique_partial
    return PersonMatch(selected=None)


def people_names_match(left: str, right: str) -> bool:
    return _score(left, right) >= 0.86


def tokens_are_close(left: str, right: str) -> bool:
    return _prefix_or_close(left, right)


def text_mentions_person(query: str, text: str) -> bool:
    """True if all query name stems appear in free text (title, outcome, …)."""
    q_stems = name_stems(query)
    hay = name_stems(text)
    if not q_stems or not hay:
        return False
    return all(
        any(_prefix_or_close(stem, other) or stem in other or other in stem for other in hay)
        for stem in q_stems
    )


def name_stems(value: str) -> tuple[str, ...]:
    return tuple(_stem(part) for part in normalize_entity_name(value).split() if part)


def _unique_partial(query: str, ranked: list[PersonHit]) -> PersonMatch | None:
    stems = name_stems(query)
    if len(stems) != 1:
        return None
    needle = stems[0]
    if len(needle) < 3:
        return None
    hits = [
        item
        for item in ranked
        if needle in name_stems(item.name) and item.score >= 0.55
    ]
    if len(hits) == 1:
        return PersonMatch(selected=hits[0])
    if len(hits) > 1:
        return PersonMatch(selected=None, candidates=tuple(hits[:8]))
    return None


def _score(query: str, canonical: str) -> float:
    q_fold = normalize_entity_name(query)
    c_fold = normalize_entity_name(canonical)
    if not q_fold or not c_fold:
        return 0.0
    if q_fold == c_fold:
        return 1.0
    q_stems = name_stems(query)
    c_stems = name_stems(canonical)
    if not q_stems or not c_stems:
        return 0.0
    if q_stems == c_stems:
        return 0.98
    if len(q_stems) >= 2 and len(c_stems) >= 2:
        if q_stems[0] == c_stems[0] and q_stems[-1] == c_stems[-1]:
            return 0.96
        if q_stems[0] == c_stems[0] and _prefix_or_close(q_stems[-1], c_stems[-1]):
            return 0.93
        if _prefix_or_close(q_stems[0], c_stems[0]) and _prefix_or_close(q_stems[-1], c_stems[-1]):
            return 0.9
    overlap = sum(1 for stem in q_stems if any(_prefix_or_close(stem, other) for other in c_stems))
    coverage = overlap / len(q_stems)
    seq = SequenceMatcher(None, q_fold, c_fold).ratio()
    first = 0.2 if _prefix_or_close(q_stems[0], c_stems[0]) else 0.0
    return min(1.0, 0.55 * coverage + 0.25 * seq + first)


def _prefix_or_close(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) >= 3 and (left.startswith(right) or right.startswith(left)):
        return True
    if min(len(left), len(right)) >= 4 and (
        left.startswith(right[:4]) or right.startswith(left[:4])
    ):
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.86


def _stem(token: str) -> str:
    text = token
    for suffix in _SUFFIXES:
        if text.endswith(suffix) and len(text) - len(suffix) >= 3:
            return text[: -len(suffix)]
    return text
