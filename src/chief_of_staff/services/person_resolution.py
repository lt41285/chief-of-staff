"""Resolve a person reference: canonical, alias, inflection, then surname."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from chief_of_staff.services.names import normalize_entity_name
from chief_of_staff.services.person_match import PersonHit, name_stems, tokens_are_close


@dataclass(frozen=True)
class PersonAliasHit:
    alias: str
    person_id: UUID
    person_name: str


@dataclass(frozen=True)
class PersonResolution:
    status: str  # resolved | ambiguous | none
    person: PersonHit | None = None
    candidates: tuple[PersonHit, ...] = ()
    via: str | None = None  # canonical | alias | variant | surname


def resolve_person_reference(
    query: str,
    people: Sequence[PersonHit],
    aliases: Sequence[PersonAliasHit] = (),
) -> PersonResolution:
    cleaned = " ".join(query.split()).strip(" .,!?:;«»\"'")
    if not cleaned:
        return PersonResolution(status="none")

    by_id: dict[UUID, PersonHit] = {}
    for person in people:
        if person.person_id is not None:
            by_id[person.person_id] = person
    for alias in aliases:
        by_id.setdefault(
            alias.person_id,
            PersonHit(name=alias.person_name, person_id=alias.person_id),
        )

    exact_canonical = [
        person for person in people if _exact_key(cleaned, person.name)
    ]
    if len(exact_canonical) == 1:
        return PersonResolution(status="resolved", person=exact_canonical[0], via="canonical")
    if len(exact_canonical) > 1:
        return PersonResolution(status="ambiguous", candidates=tuple(exact_canonical[:8]))

    exact_alias_ids = {
        alias.person_id for alias in aliases if _exact_key(cleaned, alias.alias)
    }
    exact_alias_people = _hits_for_ids(exact_alias_ids, by_id)
    if len(exact_alias_people) == 1:
        return PersonResolution(status="resolved", person=exact_alias_people[0], via="alias")
    if len(exact_alias_people) > 1:
        return PersonResolution(status="ambiguous", candidates=tuple(exact_alias_people[:8]))

    variant_ids: dict[UUID, str] = {}
    for person in people:
        if person.person_id is None:
            continue
        if inflection_variant(cleaned, person.name):
            variant_ids[person.person_id] = "variant"
    alias_variant_ids: set[UUID] = set()
    for alias in aliases:
        if inflection_variant(cleaned, alias.alias) or _exact_key(cleaned, alias.alias):
            alias_variant_ids.add(alias.person_id)
            variant_ids.setdefault(alias.person_id, "alias")
    if len(alias_variant_ids) == 1:
        person = by_id.get(next(iter(alias_variant_ids)))
        if person is not None:
            return PersonResolution(status="resolved", person=person, via="alias")

    surname = [
        person
        for person in people
        if person.person_id is not None and _surname_candidate(cleaned, person.name)
    ]
    combined_ids: dict[UUID, str] = dict(variant_ids)
    for person in surname:
        if person.person_id is not None:
            combined_ids.setdefault(person.person_id, "surname")
    combined_people = _hits_for_ids(set(combined_ids), by_id)
    if len(combined_people) == 1:
        via = combined_ids.get(combined_people[0].person_id or UUID(int=0), "variant")
        return PersonResolution(status="resolved", person=combined_people[0], via=via)
    if len(combined_people) > 1:
        return PersonResolution(status="ambiguous", candidates=tuple(combined_people[:8]))
    return PersonResolution(status="none")


def inflection_variant(query: str, stored: str) -> bool:
    """True when query is a grammatical case of the same stored label."""
    q = normalize_entity_name(query).split()
    s = normalize_entity_name(stored).split()
    if not q or not s or len(q) != len(s):
        return False
    if q == s:
        return True
    q_stems = name_stems(query)
    s_stems = name_stems(stored)
    if len(q_stems) != len(s_stems):
        return False
    return all(tokens_are_close(left, right) for left, right in zip(q_stems, s_stems))


def choose_canonical_name(names: Sequence[str]) -> str:
    ranked = sorted(
        names,
        key=lambda name: (-len(normalize_entity_name(name).split()), -len(name), name.casefold()),
    )
    return ranked[0]


def match_name_to_candidates(needle: str, candidates: Sequence[str]) -> str | None:
    hits = [name for name in candidates if inflection_variant(needle, name) or _exact_key(needle, name)]
    if len(hits) == 1:
        return hits[0]
    surname = [name for name in candidates if _surname_candidate(needle, name)]
    if len(surname) == 1:
        return surname[0]
    return hits[0] if len(hits) == 1 else None


def _exact_key(left: str, right: str) -> bool:
    return normalize_entity_name(left) == normalize_entity_name(right)


def _surname_candidate(query: str, stored: str) -> bool:
    q = normalize_entity_name(query).split()
    s = normalize_entity_name(stored).split()
    if not q or not s:
        return False
    q_stems = name_stems(query)
    s_stems = name_stems(stored)
    if len(q) == 1 and len(s) >= 2:
        return any(tokens_are_close(q_stems[0], stem) for stem in s_stems)
    if len(q) >= 2 and len(s) >= 2:
        return tokens_are_close(q_stems[-1], s_stems[-1]) and tokens_are_close(q_stems[0], s_stems[0])
    return False


def _hits_for_ids(ids: set[UUID], by_id: dict[UUID, PersonHit]) -> list[PersonHit]:
    return [by_id[item] for item in ids if item in by_id]
