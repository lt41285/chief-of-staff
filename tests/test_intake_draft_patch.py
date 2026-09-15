from datetime import date, datetime

from chief_of_staff.models.task import RequiredField, TaskDraft
from chief_of_staff.models.utterance_intent import RouterKind, UtteranceInterpretation
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.date_windows import resolve_period
from chief_of_staff.services.draft_patch import extract_utterance_patch
from chief_of_staff.services.intake_project import parse_project_ordinal, strip_project_framing
from chief_of_staff.services.pending_session import resolve_pending_control
from chief_of_staff.services.relative_deadline import resolve_deadline_expression, week_end
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_session import DraftPhase, InMemoryTaskSessionStore
from tests.fakes import FakeTaskRepository
from tests.test_reminders import FrozenClock
from tests.test_task_validation import ScriptedInterpreter, complete_draft

TURN_1 = (
    "Поговорити з господарським напрямком про те, що робити в часі жовтих травок. "
    "Проект приміщення ОКУ півгодини."
)
TURN_2 = (
    "Проєкт приміщення УКУ. Дедлайн до кінця тижня. "
    "Результат є рішення, що робити під час жовтих тривог."
)
PROJECTS = (
    "BG",
    "Unity Center",
    "Особисте",
    "Приміщення УКУ",
    "Фінанси УКУ",
)
TODAY = date(2026, 9, 15)


def _clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 9, 15, 12, 0, tzinfo=KYIV))


def _intake(
    drafts: list[TaskDraft],
) -> tuple[TaskIntakeService, InMemoryTaskSessionStore, FakeTaskRepository]:
    interpreter = ScriptedInterpreter(drafts)
    store = InMemoryTaskSessionStore()
    repo = FakeTaskRepository(known_projects=PROJECTS)
    return TaskIntakeService(interpreter, store, repo, clock=_clock()), store, repo


async def test_exact_two_turn_transcript_shows_confirmation() -> None:
    service, store, repo = _intake(
        [
            TaskDraft(
                task_title=TURN_1,
                project="приміщення ОКУ",
                estimated_minutes=30,
            ),
            TaskDraft(
                project="рішення, що робити під час жовтих тривог",
                desired_outcome="Рішення, що робити під час жовтих тривог",
            ),
        ]
    )
    first = await service.handle_user_text(1, TURN_1, chat_id=10)
    assert first.kind == IntakeKind.FOLLOW_UP
    session = store.get(1, 10)
    assert session is not None
    assert session.draft.estimated_minutes == 30
    assert session.draft.desired_outcome is None

    second = await service.handle_user_text(1, TURN_2, chat_id=10)
    assert second.kind == IntakeKind.CONFIRMATION
    assert "Приміщення УКУ" in second.text
    assert "жовтих тривог" in second.text
    assert "жовтих травок" not in second.text
    assert "ОКУ" not in second.text
    assert "Проект приміщення" not in second.text
    assert "півгодин" not in second.text.casefold()
    assert "2026-09-20" in second.text
    assert "Estimated time: 30 min" in second.text
    assert "рішення, що робити під час жовтих тривог" in second.text.casefold()
    assert "Який дедлайн" not in second.text
    draft = store.get(1, 10).draft
    assert draft.project == "Приміщення УКУ"
    assert draft.estimated_minutes == 30
    assert draft.deadline == date(2026, 9, 20)
    assert "тривог" in (draft.desired_outcome or "")
    assert "травок" not in (draft.task_title or "")
    yes = await service.handle_user_text(1, "Yes", chat_id=10)
    assert yes.kind == IntakeKind.CREATED
    assert len(repo.saved) == 1
    assert repo.saved[0].project == "Приміщення УКУ"
    assert repo.last_project_id == repo.projects["приміщення уку"].id


async def test_estimate_survives_turns_that_omit_it() -> None:
    service, store, _repo = _intake(
        [
            TaskDraft(
                task_title="Agree the budget with Taras",
                estimated_minutes=30,
                project="BG",
            ),
            TaskDraft(desired_outcome="Budget agreed", deadline=date(2026, 9, 20)),
        ]
    )
    await service.handle_user_text(1, "Agree the budget with Taras for BG, 30 min", chat_id=1)
    await service.handle_user_text(
        1, "Результат: Budget agreed. Дедлайн до кінця тижня.", chat_id=1
    )
    assert store.get(1, 1).draft.estimated_minutes == 30


async def test_outcome_never_becomes_project_candidate() -> None:
    patch = extract_utterance_patch(TURN_2, TODAY)
    assert patch.project is not None
    assert "приміщення" in patch.project.casefold()
    assert "рішення" not in patch.project.casefold()
    assert patch.desired_outcome is not None
    assert "рішення" in patch.desired_outcome.casefold()


async def test_end_of_week_resolves_without_iso_date() -> None:
    window = resolve_period("this_week", TODAY)
    assert week_end(TODAY) == window[1] == date(2026, 9, 20)
    assert resolve_deadline_expression("Дедлайн до кінця тижня.", TODAY) == date(2026, 9, 20)


async def test_project_framing_resolves_canonical() -> None:
    assert strip_project_framing("Проєкт приміщення УКУ") == "приміщення УКУ"
    service, _store, _repo = _intake(
        [complete_draft(project="Проєкт приміщення УКУ", people=())]
    )
    result = await service.handle_user_text(1, "task for project", chat_id=1)
    assert result.kind == IntakeKind.CONFIRMATION
    assert "Приміщення УКУ" in result.text


async def test_ordinal_four_selects_presented_project() -> None:
    names = list(PROJECTS)
    assert parse_project_ordinal("він під номером чотири", names) == "Приміщення УКУ"
    assert parse_project_ordinal("четвертий", names) == "Приміщення УКУ"
    service, store, _repo = _intake([complete_draft(project="Misto Hub", people=())])
    first = await service.handle_user_text(1, "task", chat_id=1)
    assert first.kind == IntakeKind.ASK_PROJECT
    assert store.get(1, 1).phase == DraftPhase.AWAITING_PROJECT
    second = await service.handle_user_text(1, "він під номером чотири", chat_id=1)
    assert second.kind == IntakeKind.CONFIRMATION
    assert "Приміщення УКУ" in second.text


async def test_later_correction_updates_title() -> None:
    service, store, _repo = _intake(
        [
            TaskDraft(
                task_title="Поговорити про жовтих травок з командою",
                project="BG",
                estimated_minutes=20,
                deadline=date(2026, 9, 20),
                desired_outcome="План дій",
            ),
            TaskDraft(),
        ]
    )
    await service.handle_user_text(1, "Поговорити про жовтих травок з командою", chat_id=1)
    await service.handle_user_text(1, "не травок, а тривог", chat_id=1)
    assert "тривог" in store.get(1, 1).draft.task_title
    assert "травок" not in store.get(1, 1).draft.task_title


async def test_later_oku_correction_updates_project() -> None:
    service, store, _repo = _intake(
        [
            complete_draft(project="приміщення ОКУ", people=()),
            TaskDraft(),
        ]
    )
    first = await service.handle_user_text(1, TURN_1, chat_id=1)
    if first.kind == IntakeKind.CONFIRMATION:
        assert store.get(1, 1).draft.project == "Приміщення УКУ"
        return
    await service.handle_user_text(1, "не ОКУ, а УКУ", chat_id=1)
    assert store.get(1, 1).draft.project == "Приміщення УКУ"


async def test_metadata_phrase_is_stripped_from_title() -> None:
    service, store, _repo = _intake(
        [
            TaskDraft(
                task_title=TURN_1,
                project="Приміщення УКУ",
                estimated_minutes=30,
                deadline=date(2026, 9, 20),
                desired_outcome="Рішення, що робити під час жовтих тривог",
            )
        ]
    )
    result = await service.handle_user_text(1, TURN_1, chat_id=1)
    assert result.kind == IntakeKind.CONFIRMATION
    title = store.get(1, 1).draft.task_title or ""
    assert "Проект" not in title and "Проєкт" not in title
    assert "півгодин" not in title.casefold()
    assert "Поговорити з господарським напрямком" in title


async def test_missing_fields_in_later_utterance_do_not_erase() -> None:
    service, store, _repo = _intake(
        [
            complete_draft(desired_outcome=None, people=()),
            TaskDraft(desired_outcome="Done looks like a signed note"),
        ]
    )
    await service.handle_user_text(1, "Agree the budget", chat_id=1)
    before = store.get(1, 1).draft
    await service.handle_user_text(1, "Done looks like a signed note", chat_id=1)
    after = store.get(1, 1).draft
    assert after.estimated_minutes == before.estimated_minutes == 20
    assert after.project == before.project
    assert after.deadline == before.deadline


async def test_voice_and_text_share_the_same_patch_pipeline() -> None:
    text_service, text_store, _ = _intake(
        [
            TaskDraft(task_title=TURN_1, project="приміщення ОКУ", estimated_minutes=30),
            TaskDraft(desired_outcome="Рішення, що робити під час жовтих тривог"),
        ]
    )
    voice_service, voice_store, _ = _intake(
        [
            TaskDraft(task_title=TURN_1, project="приміщення ОКУ", estimated_minutes=30),
            TaskDraft(desired_outcome="Рішення, що робити під час жовтих тривог"),
        ]
    )
    await text_service.handle_user_text(1, TURN_1, chat_id=1)
    await voice_service.handle_user_text(1, TURN_1, chat_id=1)
    await text_service.handle_user_text(1, TURN_2, chat_id=1)
    await voice_service.handle_user_text(1, TURN_2, chat_id=1)
    assert text_store.get(1, 1).draft.model_dump() == voice_store.get(1, 1).draft.model_dump()


async def test_pending_create_task_routes_to_patch_not_provide() -> None:
    pending = {
        "pending_action": "create_task",
        "awaiting": "desired_outcome,project,deadline",
    }
    control = resolve_pending_control(
        UtteranceInterpretation(kind=RouterKind.CREATE_TASK, project="рішення"),
        pending,
        TURN_2,
    )
    assert control == "patch"
    unclear = resolve_pending_control(
        UtteranceInterpretation(kind=RouterKind.UNCLEAR),
        pending,
        "я вже казав про це в попередньому повідомленні",
    )
    assert unclear == "patch"


def test_required_fields_come_from_accumulated_draft() -> None:
    from chief_of_staff.services.task_validation import missing_required_fields

    draft = TaskDraft(
        task_title="Поговорити з господарським напрямком про жовті тривоги",
        project="Приміщення УКУ",
        estimated_minutes=30,
        deadline=date(2026, 9, 20),
        desired_outcome="Рішення, що робити під час жовтих тривог",
    )
    assert missing_required_fields(draft) == ()
    assert RequiredField.ESTIMATED_MINUTES not in missing_required_fields(
        draft.model_copy(update={"desired_outcome": None})
    )
