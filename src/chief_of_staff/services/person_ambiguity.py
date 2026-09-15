"""Parse explicit person identity replies while an ambiguity question is pending."""

import re

from chief_of_staff.services.conversation_context import PendingAmbiguity
from chief_of_staff.services.person_resolution import choose_canonical_name, match_name_to_candidates

_APOS = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})

_SAME = re.compile(
    r"(?i)"
    r"одне\s+і\s+те\s+ж|"
    r"одне\s+й\s+те\s+ж|"
    r"та\s+сама\s+людина|"
    r"той\s+самий|"
    r"одна\s+людина|"
    r"вони\s+одна|"
    r"це\s+той\s+самий|"
    r"same\s+person|"
    r"they(?:'re|\s+are)\s+the\s+same"
)
_EQUALS = re.compile(
    r"(?i)^\s*(?P<left>.+?)\s*(?:—|-|–|=|це)\s*(?P<right>.+?)\s*$"
)
_MEAN = re.compile(
    r"(?i)(?:коли\s+я\s+кажу|якщо\s+кажу|під)\s+(?P<alias>.+?)"
    r"\s*,?\s*(?:маю\s+на\s+увазі|це)\s+(?P<canonical>.+?)\s*$"
)
_MEAN_SHORT = re.compile(
    r"(?i)я\s+маю\s+на\s+увазі\s+(?P<canonical>.+?)\s*$"
)
_THIS_IS = re.compile(
    r"(?i)^\s*це\s+(?P<name>.+?)\s*$"
)


def parse_person_ambiguity_reply(
    text: str, pending: PendingAmbiguity | None
) -> dict[str, object] | None:
    if pending is None or not pending.candidates:
        return None
    raw = " ".join(text.translate(_APOS).split()).strip().strip(" .!?")
    if not raw:
        return None
    candidates = pending.candidates

    mean = _MEAN.match(raw)
    if mean:
        resolved = _pair(_clean(mean.group("alias")), _clean(mean.group("canonical")), candidates)
        if resolved is not None:
            return {"action": "merge", **resolved}

    equals = _EQUALS.match(raw)
    if equals and len(raw) > 6:
        resolved = _pair(_clean(equals.group("left")), _clean(equals.group("right")), candidates)
        if resolved is not None:
            return {"action": "merge", **resolved}

    if _SAME.search(raw):
        canonical = choose_canonical_name(candidates)
        others = [name for name in candidates if name != canonical]
        return {
            "action": "merge",
            "canonical": canonical,
            "alias": others[0] if others else pending.original_reference,
        }

    mean_short = _MEAN_SHORT.match(raw)
    if mean_short:
        picked = match_name_to_candidates(_clean(mean_short.group("canonical")), candidates)
        if picked is not None:
            return {"action": "pick", "name": picked}

    this_is = _THIS_IS.match(raw)
    if this_is:
        picked = match_name_to_candidates(_clean(this_is.group("name")), candidates)
        if picked is not None:
            return {"action": "pick", "name": picked}

    ordinal = _ordinal_index(raw)
    if ordinal is not None and 1 <= ordinal <= len(candidates):
        return {"action": "pick", "name": candidates[ordinal - 1]}
    return None


def _pair(left: str, right: str, candidates: tuple[str, ...]) -> dict[str, str] | None:
    hits: list[str] = []
    for part in (left, right):
        hit = match_name_to_candidates(part, candidates)
        if hit is not None:
            hits.append(hit)
    unique = list(dict.fromkeys(hits))
    if len(unique) != 2:
        return None
    canonical = choose_canonical_name(unique)
    alias = unique[1] if unique[0] == canonical else unique[0]
    return {"canonical": canonical, "alias": alias}


def _ordinal_index(raw: str) -> int | None:
    folded = raw.casefold()
    if re.match(r"(?i)^(перш[еа]|1(?:-?[еа])?|first)$", folded):
        return 1
    if re.match(r"(?i)^(друг[еа]|2(?:-?[еа])?|second)$", folded):
        return 2
    if re.match(r"(?i)^(трет[єе]|3(?:-?[єе])?|third)$", folded):
        return 3
    return None


def _clean(value: str) -> str:
    return value.strip(" «»\"'.,;:")
