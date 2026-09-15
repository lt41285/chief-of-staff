"""Maps user text to a TaskDraft via OpenAI structured output."""

from datetime import datetime

from chief_of_staff.infrastructure.openai.client import OpenAIResponsesClient
from chief_of_staff.models.task import RequiredField, TaskDraft, TaskExtractionSchema
from chief_of_staff.prompts.task_extraction import TASK_EXTRACTION_INSTRUCTIONS
from chief_of_staff.services.clock import Clock
from chief_of_staff.services.task_validation import draft_from_extraction, format_kyiv_clock


class TaskInterpreter:
    def __init__(self, client: OpenAIResponsesClient, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    async def extract(
        self,
        user_text: str,
        *,
        current_draft: TaskDraft | None = None,
        missing: tuple[RequiredField, ...] = (),
        now: datetime | None = None,
    ) -> TaskDraft:
        moment = now or self._clock.now()
        parts = [
            f"Current date/time in Europe/Kyiv: {format_kyiv_clock(moment)}",
        ]
        if current_draft is not None:
            parts.append(
                "Already captured draft (return null for these unless the user "
                f"explicitly corrects them):\n{current_draft.model_dump_json()}"
            )
        if missing:
            parts.append("Still missing and must be filled from this message if possible: "
                         + ", ".join(field.value for field in missing))
        parts.append(f"User message:\n{user_text}")
        schema = await self._client.parse_structured(
            user_input="\n\n".join(parts),
            instructions=TASK_EXTRACTION_INSTRUCTIONS,
            text_format=TaskExtractionSchema,
        )
        return draft_from_extraction(schema, today=moment.date())
