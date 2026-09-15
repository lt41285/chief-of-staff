from unittest.mock import patch

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_session import DraftPhase, InMemoryTaskSessionStore
from chief_of_staff.services.voice import TRANSCRIBE_FAILED, VoiceMessageService, format_heard_message
from tests.fakes import FakeTaskRepository
from tests.test_task_validation import ScriptedInterpreter, complete_draft


class FakeTranscriber:
    def __init__(self, outputs: list[str] | Exception) -> None:
        self.calls: list[tuple[bytes, str]] = []
        self._outputs = outputs

    async def transcribe_audio(self, audio: bytes, filename: str = "voice.ogg") -> str:
        self.calls.append((audio, filename))
        if isinstance(self._outputs, Exception):
            raise self._outputs
        if not self._outputs:
            raise AssertionError("unexpected transcribe call")
        return self._outputs.pop(0)


def _voice_service(
    interpreter: ScriptedInterpreter,
    transcriber: FakeTranscriber,
    *,
    store: InMemoryTaskSessionStore | None = None,
    repo: FakeTaskRepository | None = None,
) -> tuple[VoiceMessageService, TaskIntakeService, InMemoryTaskSessionStore, FakeTaskRepository]:
    store = store or InMemoryTaskSessionStore()
    repo = repo or FakeTaskRepository()
    intake = TaskIntakeService(interpreter, store, repo)
    return VoiceMessageService(transcriber, intake), intake, store, repo


async def test_voice_transcription_goes_to_existing_task_intake() -> None:
    transcript = (
        "По Unity Center треба поговорити з Тарасом про бюджет до середи. "
        "Це терміново і важливо. Думаю хвилин двадцять."
    )
    interpreter = ScriptedInterpreter([complete_draft(desired_outcome=None)])
    transcriber = FakeTranscriber([transcript])
    voice, intake, _, _ = _voice_service(interpreter, transcriber)

    result = await voice.handle_voice(1, 10, b"ogg-bytes")
    assert result.error is None
    assert result.heard_text == format_heard_message(transcript)
    assert result.intake is not None
    assert result.intake.kind == IntakeKind.FOLLOW_UP
    assert "importance" not in result.intake.text.casefold()
    assert "urgency" not in result.intake.text.casefold()
    assert "важлив" not in result.intake.text.casefold()
    assert "термінов" not in result.intake.text.casefold()
    assert interpreter.calls[0]["user_text"] == transcript
    assert transcriber.calls[0] == (b"ogg-bytes", "voice.ogg")


async def test_ukrainian_transcription_is_not_translated() -> None:
    uk = "Бюджет погоджено з Тарасом"
    interpreter = ScriptedInterpreter([complete_draft()])
    voice, _, _, _ = _voice_service(interpreter, FakeTranscriber([uk]))
    result = await voice.handle_voice(2, 20, b"audio")
    assert result.error is None
    assert result.heard_text is not None
    assert uk in result.heard_text
    assert "Budget" not in result.heard_text
    assert interpreter.calls[0]["user_text"] == uk


async def test_voice_follow_up_updates_existing_draft() -> None:
    store = InMemoryTaskSessionStore()
    interpreter = ScriptedInterpreter(
        [
            complete_draft(desired_outcome=None),
            complete_draft(),
        ]
    )
    intake = TaskIntakeService(interpreter, store, FakeTaskRepository())
    first = await process_user_utterance(intake, 3, 30, "text first turn")
    assert first.kind == IntakeKind.FOLLOW_UP
    draft_after_text = store.get(3, 30)
    assert draft_after_text is not None

    transcriber = FakeTranscriber(["Бюджет погоджено з Тарасом"])
    voice = VoiceMessageService(transcriber, intake)
    second = await voice.handle_voice(3, 30, b"follow-up")
    assert second.intake is not None
    assert second.intake.kind == IntakeKind.CONFIRMATION
    assert "Який результат" not in (second.intake.text)
    session = store.get(3, 30)
    assert session is not None
    assert session.phase == DraftPhase.CONFIRMING
    assert session.draft.project == "Unity Center"


async def test_transcription_failure_retains_existing_draft() -> None:
    store = InMemoryTaskSessionStore()
    interpreter = ScriptedInterpreter([complete_draft(desired_outcome=None)])
    intake = TaskIntakeService(interpreter, store, FakeTaskRepository())
    await process_user_utterance(intake, 4, 40, "seed draft")
    before = store.get(4, 40)
    assert before is not None

    voice = VoiceMessageService(FakeTranscriber(RuntimeError("stt down")), intake)
    result = await voice.handle_voice(4, 40, b"bad")
    assert result.error == TRANSCRIBE_FAILED
    assert result.intake is None
    after = store.get(4, 40)
    assert after is not None
    assert after.draft == before.draft
    assert after.phase == before.phase


async def test_empty_transcription_does_not_touch_draft() -> None:
    store = InMemoryTaskSessionStore()
    interpreter = ScriptedInterpreter([complete_draft(desired_outcome=None)])
    intake = TaskIntakeService(interpreter, store, FakeTaskRepository())
    await process_user_utterance(intake, 5, 50, "seed")
    voice = VoiceMessageService(FakeTranscriber([""]), intake)
    result = await voice.handle_voice(5, 50, b"silence")
    assert result.error == TRANSCRIBE_FAILED
    assert store.get(5, 50) is not None
    assert interpreter.calls  # only the seed text turn
    assert len(interpreter.calls) == 1


async def test_no_duplicate_task_creation_across_voice_and_text() -> None:
    repo = FakeTaskRepository()
    store = InMemoryTaskSessionStore()
    interpreter = ScriptedInterpreter(
        [
            complete_draft(desired_outcome=None),
            complete_draft(),
        ]
    )
    intake = TaskIntakeService(interpreter, store, repo)
    voice = VoiceMessageService(FakeTranscriber(["voice describing the task"]), intake)
    await voice.handle_voice(6, 60, b"v1")
    await process_user_utterance(intake, 6, 60, "Revised budget is agreed with Taras")
    yes = await process_user_utterance(intake, 6, 60, "Yes")
    assert yes.kind == IntakeKind.CREATED
    assert len(repo.saved) == 1


async def test_voice_and_text_use_the_same_downstream_function() -> None:
    interpreter = ScriptedInterpreter([complete_draft(), complete_draft()])
    transcriber = FakeTranscriber(["from voice"])
    voice, intake, _, _ = _voice_service(interpreter, transcriber)
    seen: list[str] = []

    async def wrapped(svc, user_id, chat_id, text, **kwargs):
        seen.append(text)
        return await original(svc, user_id, chat_id, text, **kwargs)

    original = process_user_utterance
    with patch("chief_of_staff.services.voice.process_user_utterance", wrapped):
        await voice.handle_voice(7, 70, b"v")
    await process_user_utterance(intake, 7, 71, "from text")
    assert seen == ["from voice"]
    assert interpreter.calls[0]["user_text"] == "from voice"
    assert interpreter.calls[1]["user_text"] == "from text"
    from chief_of_staff.bot.handlers import task as task_handlers
    from chief_of_staff.services import voice as voice_module

    assert task_handlers.process_user_utterance is original
    assert voice_module.process_user_utterance is original
