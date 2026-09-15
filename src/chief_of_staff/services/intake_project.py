"""Resolve intake project names against the user's existing projects."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from uuid import UUID

from chief_of_staff.services.names import (
    core_project_key,
    projects_look_similar,
)

_FRAME = re.compile(
    r"(?i)^(?:це\s+)?(?:для\s+)?(?:проєкти|проекти|проєкту|проекту|проєкт|проект)\s+"
)
_UK_ORDINALS = {
    "перш": 1,
    "друг": 2,
    "трет": 3,
    "четверт": 4,
    "п'ят": 5,
    "п’ят": 5,
    "шост": 6,
    "сьом": 7,
    "восьм": 8,
    "дев'ят": 9,
    "дев’ят": 9,
    "десят": 10,
}
_EN_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}
_UK_CARDINAL = {
    "один": 1,
    "одна": 1,
    "одну": 1,
    "два": 2,
    "дві": 2,
    "три": 3,
    "чотири": 4,
    "п'ять": 5,
    "п’ять": 5,
    "шість": 6,
    "сім": 7,
    "вісім": 8,
    "дев'ять": 9,
    "дев’ять": 9,
    "десять": 10,
}
UNIQUE_RATIO = 0.82
UNIQUE_GAP = 0.08


@dataclass(frozen=True)
class CatalogProject:
    id: UUID
    name: str


@dataclass(frozen=True)
class ProjectMatch:
    status: str  # resolved | confirm | unknown | ordinal_miss
    name: str | None = None
    project_id: UUID | None = None
    asked: str | None = None


def strip_project_framing(name: str) -> str:
    text = " ".join(name.split()).strip(" .,:;")
    text = _FRAME.sub("", text).strip(" .,:;")
    return text


def parse_project_ordinal(text: str, presented: tuple[str, ...] | list[str]) -> str | None:
    names = list(presented)
    if not names:
        return None
    folded = " ".join(text.casefold().split())
    number = None
    digit = re.search(r"(?:номер(?:ом)?|під\s+номером|#|№)\s*(\d+)", folded)
    if digit:
        number = int(digit.group(1))
    if number is None:
        trailing = re.search(r"\b(\d+)\b", folded)
        if trailing and re.search(r"(?i)номер|четверт|перш|друг|трет|#", folded):
            number = int(trailing.group(1))
    if number is None:
        for prefix, value in _UK_ORDINALS.items():
            if re.search(rf"\b{re.escape(prefix)}\w*", folded):
                number = value
                break
    if number is None:
        for word, value in _EN_ORDINALS.items():
            if re.search(rf"\b{word}\b", folded):
                number = value
                break
    if number is None:
        numbered = re.search(r"(?:номер(?:ом)?|під\s+номером)\s+(\w+)", folded)
        if numbered:
            number = _UK_CARDINAL.get(numbered.group(1))
    if number is None:
        return None
    if 1 <= number <= len(names):
        return names[number - 1]
    return None


def best_unique_similar(query: str, catalog: list[CatalogProject]) -> CatalogProject | None:
    if not query or not catalog:
        return None
    scored: list[tuple[float, CatalogProject]] = []
    q = core_project_key(query) or query.casefold()
    for item in catalog:
        if projects_look_similar(query, item.name):
            scored.append((1.0, item))
            continue
        name_key = core_project_key(item.name) or item.name.casefold()
        ratio = SequenceMatcher(None, q, name_key).ratio()
        if q and name_key and (q in name_key or name_key in q) and min(len(q), len(name_key)) >= 4:
            ratio = max(ratio, 0.9)
        scored.append((ratio, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored:
        return None
    top_ratio, top = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if top_ratio >= UNIQUE_RATIO and (top_ratio - second) >= UNIQUE_GAP:
        return top
    return None


async def resolve_intake_project(
    repository: object,
    user_id: int,
    raw: str,
    *,
    presented: tuple[str, ...] = (),
) -> ProjectMatch:
    asked = strip_project_framing(raw)
    if not asked:
        return ProjectMatch(status="unknown", asked=raw)
    names = list(presented)
    if not names:
        list_fn = getattr(repository, "list_projects_for_user", None)
        if list_fn is not None:
            names = [name for name, _count in await list_fn(user_id)]
    ordinal = parse_project_ordinal(raw, names)
    if ordinal:
        resolve = getattr(repository, "resolve_user_project", None)
        if resolve is not None:
            hit = await resolve(user_id, ordinal)
            if hit is not None:
                return ProjectMatch(status="resolved", name=hit.name, project_id=hit.id, asked=asked)
        return ProjectMatch(status="resolved", name=ordinal, asked=asked)
    resolve = getattr(repository, "resolve_user_project", None)
    if resolve is not None:
        exact = await resolve(user_id, asked)
        if exact is not None:
            return ProjectMatch(status="resolved", name=exact.name, project_id=exact.id, asked=asked)
        framed = await resolve(user_id, raw)
        if framed is not None:
            return ProjectMatch(status="resolved", name=framed.name, project_id=framed.id, asked=asked)
    catalog: list[CatalogProject] = []
    list_fn = getattr(repository, "list_projects_for_user", None)
    if list_fn is not None:
        rows = await list_fn(user_id)
        resolve = getattr(repository, "resolve_user_project", None)
        for name, _count in rows:
            if resolve is not None:
                hit = await resolve(user_id, name)
                if hit is not None:
                    catalog.append(CatalogProject(id=hit.id, name=hit.name))
                    continue
            catalog.append(CatalogProject(id=UUID(int=0), name=name))
    similar = best_unique_similar(asked, catalog)
    if similar is not None and similar.id and similar.id != UUID(int=0):
        return ProjectMatch(
            status="resolved",
            name=similar.name,
            project_id=similar.id,
            asked=asked,
        )
    suggest = getattr(repository, "suggest_similar_project", None)
    if suggest is not None:
        hinted = await suggest(user_id, asked)
        if hinted is not None:
            return ProjectMatch(status="confirm", name=hinted.name, project_id=hinted.id, asked=asked)
    return ProjectMatch(status="unknown", asked=asked)
