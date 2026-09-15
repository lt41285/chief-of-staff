"""Thin wrapper around the OpenAI Responses API and speech-to-text."""

from io import BytesIO

from openai import AsyncOpenAI
from pydantic import BaseModel

from chief_of_staff.config.settings import Settings, get_settings

_TRANSCRIBE_PROMPT = (
    "Transcribe the speech verbatim in the original language "
    "(Ukrainian or English). Do not translate."
)


class OpenAIResponsesClient:
    """Creates model responses via `client.responses.create` / `.parse`."""

    def __init__(self, settings: Settings | None = None) -> None:
        cfg = settings or get_settings()
        self._model = cfg.openai_model
        self._transcription_model = cfg.openai_transcription_model
        self._client = AsyncOpenAI(api_key=cfg.require_openai_api_key())

    async def create_response(self, user_input: str, instructions: str | None = None) -> str:
        response = await self._client.responses.create(
            model=self._model,
            input=user_input,
            instructions=instructions,
        )
        return response.output_text

    async def parse_structured[T: BaseModel](
        self,
        *,
        user_input: str,
        instructions: str,
        text_format: type[T],
    ) -> T:
        response = await self._client.responses.parse(
            model=self._model,
            input=user_input,
            instructions=instructions,
            text_format=text_format,
            temperature=0,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise ValueError("OpenAI returned no structured output")
        return parsed

    async def transcribe_audio(self, audio: bytes, filename: str = "voice.ogg") -> str:
        buffer = BytesIO(audio)
        buffer.name = filename
        result = await self._client.audio.transcriptions.create(
            model=self._transcription_model,
            file=buffer,
            prompt=_TRANSCRIBE_PROMPT,
        )
        return (result.text or "").strip()
