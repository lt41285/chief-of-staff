"""Voice messages: transcribe, then reuse the text task-intake flow."""

from dataclasses import dataclass
from typing import Protocol

from loguru import logger

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.services.conversation_context import InMemoryConversationStore
from chief_of_staff.services.daily_planning import DailyPlanningService, PlanResult
from chief_of_staff.services.intent_router import IntentRouter
from chief_of_staff.services.project_ops import ProjectManagementService, ProjectResult
from chief_of_staff.services.task_intake import IntakeResult, TaskIntakeService
from chief_of_staff.services.task_lifecycle import LifecycleResult, TaskLifecycleService
from chief_of_staff.services.task_query import QueryResult, TaskQueryService

TRANSCRIBE_FAILED = "Не вдалося розпізнати голосове повідомлення. Спробуй ще раз."


class SpeechToText(Protocol):
    async def transcribe_audio(self, audio: bytes, filename: str = "voice.ogg") -> str: ...


@dataclass(frozen=True)
class VoiceHandleResult:
    heard_text: str | None
    intake: IntakeResult | PlanResult | ProjectResult | LifecycleResult | QueryResult | None
    error: str | None


def format_heard_message(transcript: str) -> str:
    return f'🎙 Я почув:\n"{transcript}"'


class VoiceMessageService:
    def __init__(
        self,
        transcriber: SpeechToText,
        intake: TaskIntakeService,
        planning: DailyPlanningService | None = None,
        projects: ProjectManagementService | None = None,
        lifecycle: TaskLifecycleService | None = None,
        queries: TaskQueryService | None = None,
        router: IntentRouter | None = None,
        context: InMemoryConversationStore | None = None,
    ) -> None:
        self._transcriber = transcriber
        self._intake = intake
        self._planning = planning
        self._projects = projects
        self._lifecycle = lifecycle
        self._queries = queries
        self._router = router
        self._context = context

    async def handle_voice(
        self,
        user_id: int,
        chat_id: int,
        audio: bytes,
        *,
        filename: str = "voice.ogg",
        duration_seconds: int | None = None,
    ) -> VoiceHandleResult:
        logger.info(
            "Voice received user_id={user_id} chat_id={chat_id} bytes={size} duration_s={duration}",
            user_id=user_id,
            chat_id=chat_id,
            size=len(audio),
            duration=duration_seconds,
        )
        try:
            transcript = await self._transcriber.transcribe_audio(audio, filename)
        except Exception:
            logger.exception(
                "Voice transcription failed user_id={user_id} chat_id={chat_id}",
                user_id=user_id,
                chat_id=chat_id,
            )
            return VoiceHandleResult(heard_text=None, intake=None, error=TRANSCRIBE_FAILED)

        if not transcript:
            logger.warning(
                "Voice transcription empty user_id={user_id} chat_id={chat_id}",
                user_id=user_id,
                chat_id=chat_id,
            )
            return VoiceHandleResult(heard_text=None, intake=None, error=TRANSCRIBE_FAILED)

        logger.info(
            "Voice transcribed user_id={user_id} chat_id={chat_id} chars={chars}",
            user_id=user_id,
            chat_id=chat_id,
            chars=len(transcript),
        )
        intake_result = await process_user_utterance(
            self._intake,
            user_id,
            chat_id,
            transcript,
            planning=self._planning,
            projects=self._projects,
            lifecycle=self._lifecycle,
            queries=self._queries,
            router=self._router,
            context=self._context,
        )
        return VoiceHandleResult(
            heard_text=format_heard_message(transcript),
            intake=intake_result,
            error=None,
        )
