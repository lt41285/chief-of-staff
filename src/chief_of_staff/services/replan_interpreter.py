"""Interpret replan preferences into structured constraints."""

from chief_of_staff.infrastructure.openai.client import OpenAIResponsesClient
from chief_of_staff.models.plan import PlanConstraints, ReplanConstraintsSchema
from chief_of_staff.prompts.replan import REPLAN_INSTRUCTIONS


class ReplanInterpreter:
    def __init__(self, client: OpenAIResponsesClient) -> None:
        self._client = client

    async def interpret(self, text: str) -> PlanConstraints:
        schema = await self._client.parse_structured(
            user_input=text,
            instructions=REPLAN_INSTRUCTIONS,
            text_format=ReplanConstraintsSchema,
        )
        extra = schema.extra_reserve_minutes
        if extra is not None and extra < 0:
            extra = None
        return PlanConstraints(
            exclude_projects=tuple(p.strip() for p in schema.exclude_projects if p.strip()),
            exclude_people=tuple(p.strip() for p in schema.exclude_people if p.strip()),
            extra_reserve_minutes=extra,
            prefer_short_tasks=schema.prefer_short_tasks,
        )


def merge_constraints(base: PlanConstraints, incoming: PlanConstraints) -> PlanConstraints:
    extra = incoming.extra_reserve_minutes
    if extra is None:
        extra = base.extra_reserve_minutes
    return PlanConstraints(
        exclude_projects=_unique(base.exclude_projects + incoming.exclude_projects),
        exclude_people=_unique(base.exclude_people + incoming.exclude_people),
        extra_reserve_minutes=extra,
        prefer_short_tasks=base.prefer_short_tasks or incoming.prefer_short_tasks,
    )


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return tuple(out)
