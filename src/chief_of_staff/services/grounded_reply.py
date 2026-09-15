"""Validate that an AI reply only cites this-turn Python facts."""

from uuid import UUID

from chief_of_staff.models.utterance_intent import GroundedReply
from chief_of_staff.services.task_query import QueryResult


def validate_grounded_reply(
    reply: GroundedReply,
    result: QueryResult,
) -> str | None:
    """Return the message if it is grounded; otherwise None (use Python text)."""
    message = (reply.message or "").strip()
    if not message:
        return None
    allowed = {str(task_id) for task_id in result.task_ids}
    referenced: list[str] = []
    for raw in reply.referenced_task_ids:
        token = str(raw).strip()
        if not token:
            continue
        referenced.append(token)
        if token not in allowed:
            return None
    if allowed and not referenced:
        return None
    if reply.stated_count is not None and reply.stated_count != len(result.task_ids):
        return None
    if (
        reply.stated_total_minutes is not None
        and result.total_estimated_minutes is not None
        and reply.stated_total_minutes != result.total_estimated_minutes
    ):
        return None
    for maybe_id in _uuids_in_text(message):
        if maybe_id not in allowed:
            return None
    return message


def _uuids_in_text(text: str) -> list[str]:
    found: list[str] = []
    for token in text.replace(",", " ").split():
        cleaned = token.strip(" .;:()[]«»\"'")
        try:
            found.append(str(UUID(cleaned)))
        except ValueError:
            continue
    return found
