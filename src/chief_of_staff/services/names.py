"""Normalize entity names for reuse (projects, people)."""

from difflib import SequenceMatcher
import re
import unicodedata

_PROJECT_NOISE = re.compile(
    r"^(?:проєкти|проєкт|проекти|проект|projects?)\s+|"
    r"\s+(?:проєкти|проєкт|проекти|проект|projects?)$",
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r"[\s.\-_·•]+")

SIMILARITY_THRESHOLD = 0.86
_ACRONYM_MIN = 2
_ACRONYM_MAX = 6


def normalize_entity_name(name: str) -> str:
    """People and generic fold: unicode, trim, collapse spaces, casefold."""
    return _basic_fold(name)


def normalize_project_name(name: str) -> str:
    """Matching key for projects and aliases. Does not change display names.

    Short Latin acronyms (2–6 letters) ignore case and separators so
    BG / Bg / B G / B.G. / B-G all become ``bg``.
    """
    folded = _basic_fold(name)
    acronym = _latin_acronym_key(folded)
    if acronym is not None:
        return acronym
    return folded


def core_project_key(name: str) -> str:
    """Strip leading/trailing 'project' words so 'проєкти BG' and 'BG' share a core."""
    stripped = _PROJECT_NOISE.sub("", _basic_fold(name)).strip()
    acronym = _latin_acronym_key(stripped)
    if acronym is not None:
        return acronym
    return stripped


def display_project_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name)
    return " ".join(text.split()).strip()


def projects_look_similar(left: str, right: str) -> bool:
    """Heuristic for a duplicate *suggestion*. Never used to auto-merge."""
    if normalize_project_name(left) == normalize_project_name(right):
        return True
    a = _basic_fold(left)
    b = _basic_fold(right)
    if not a or not b:
        return False
    ca, cb = core_project_key(left), core_project_key(right)
    if ca and cb and ca == cb:
        return True
    if ca and cb and (ca in cb or cb in ca) and min(len(ca), len(cb)) >= 2:
        return True
    return SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD


def _basic_fold(name: str) -> str:
    text = unicodedata.normalize("NFKC", name)
    return " ".join(text.split()).casefold()


def _latin_acronym_key(folded: str) -> str | None:
    """Match short spelled-out or compact Latin acronyms, not ordinary words.

    ``BG`` / ``B G`` / ``B.G.`` share a key. ``Go Pro`` and ``AB Corp`` do not.
    """
    compact = _SEPARATORS.sub("", folded)
    if not (
        _ACRONYM_MIN <= len(compact) <= _ACRONYM_MAX
        and compact.isascii()
        and compact.isalpha()
    ):
        return None
    if _SEPARATORS.search(folded) is None:
        return compact
    tokens = [part for part in _SEPARATORS.split(folded) if part]
    if tokens and all(len(part) == 1 for part in tokens):
        return compact
    return None
