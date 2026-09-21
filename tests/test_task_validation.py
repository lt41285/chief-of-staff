from datetime import date

from chief_of_staff.models.task import RequiredField, TaskDraft, TaskExtractionSchema
from chief_of_staff.services.task_card import format_confirmation_card
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_questions import follow_up_questions
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.task_validation import (
    draft_from_extraction,
    fill_missing_from_user_reply,
    is_clear_smart_text,
    is_ready,
    merge_drafts,
    missing_required_fields,
    parse_deadline,
)


from tests.fakes import FakeTaskRepository


def complete_draft(**overrides: object) -> TaskDraft:
    data: dict[str, object] = {
        "task_title": "Agree the revised budget with Taras",
        "project": "Unity Center",
        "people": ("Taras",),
        "deadline": date(2026, 9, 2),
        "estimated_minutes": 20,
        "desired_outcome": "Revised budget is agreed",
    }
    data.update(overrides)
    return TaskDraft.model_validate(data)


class ScriptedInterpreter:
    def __init__(self, drafts: list[TaskDraft]) -> None:
        self._drafts = list(drafts)
        self.calls: list[dict[str, object]] = []

    async def extract(self, user_text: str, **kwargs: object) -> TaskDraft:
        self.calls.append({"user_text": user_text, **kwargs})
        if not self._drafts:
            raise AssertionError(f"unexpected extract call for {user_text!r}")
        return self._drafts.pop(0)


def make_intake(interpreter: ScriptedInterpreter) -> TaskIntakeService:
    return TaskIntakeService(interpreter, InMemoryTaskSessionStore(), FakeTaskRepository())


def test_complete_draft_is_ready() -> None:
    assert is_ready(complete_draft())
    assert missing_required_fields(complete_draft()) == ()


def test_people_are_not_required() -> None:
    assert is_ready(complete_draft(people=()))


def test_missing_project() -> None:
    draft = complete_draft(project=None)
    assert RequiredField.PROJECT in missing_required_fields(draft)
    assert not is_ready(draft)


def test_missing_deadline() -> None:
    assert RequiredField.DEADLINE in missing_required_fields(complete_draft(deadline=None))


def test_missing_importance_and_urgency_are_not_required() -> None:
    draft = complete_draft(importance=None, urgency=None)
    missing = missing_required_fields(draft)
    assert is_ready(draft)
    assert missing == ()


def test_bot_never_asks_importance_or_urgency() -> None:
    questions = follow_up_questions(
        (
            RequiredField.TASK_TITLE,
            RequiredField.DESIRED_OUTCOME,
            RequiredField.PROJECT,
            RequiredField.DEADLINE,
            RequiredField.ESTIMATED_MINUTES,
        ),
        last_user_text="Please handle this",
    )
    blob = " ".join(questions).casefold()
    assert "importance" not in blob
    assert "urgency" not in blob
    assert "важлив" not in blob
    assert "urgent" not in blob


def test_missing_estimated_minutes() -> None:
    assert RequiredField.ESTIMATED_MINUTES in missing_required_fields(
        complete_draft(estimated_minutes=None)
    )


def test_vague_title_is_not_ready() -> None:
    draft = complete_draft(task_title="todo")
    assert RequiredField.TASK_TITLE in missing_required_fields(draft)
    assert not is_clear_smart_text("call")
    assert not is_clear_smart_text("задача")


def test_missing_desired_outcome() -> None:
    assert RequiredField.DESIRED_OUTCOME in missing_required_fields(
        complete_draft(desired_outcome=None)
    )


def test_invalid_deadline_is_dropped() -> None:
    schema = TaskExtractionSchema(deadline="Friday", estimated_minutes=20)
    draft = draft_from_extraction(schema)
    assert draft.deadline is None
    assert parse_deadline("2026-09-02") == date(2026, 9, 2)


def test_non_positive_estimate_is_dropped() -> None:
    draft = draft_from_extraction(TaskExtractionSchema(estimated_minutes=0))
    assert draft.estimated_minutes is None


def test_people_extracted_separately_and_deduped() -> None:
    schema = TaskExtractionSchema(people=["Taras", " taras ", "", "Oksana"])
    draft = draft_from_extraction(schema)
    assert draft.people == ("Taras", "Oksana")


def test_merge_does_not_invent_or_wipe_with_nulls() -> None:
    base = complete_draft(project="Unity Center", deadline=None)
    incoming = TaskDraft(project=None, deadline=date(2026, 9, 2), people=("Taras",))
    merged = merge_drafts(base, incoming)
    assert merged.project == "Unity Center"
    assert merged.deadline == date(2026, 9, 2)
    assert "Taras" in merged.people
    assert merged.task_title == base.task_title


def test_merge_stated_fields_replace_but_nulls_do_not_wipe() -> None:
    base = complete_draft(desired_outcome=None)
    incoming = TaskDraft(
        task_title="Short rewrite that would fail later",
        desired_outcome="Revised budget is agreed with Taras",
        project="Wrong project",
    )
    merged = merge_drafts(base, incoming)
    assert merged.task_title == "Short rewrite that would fail later"
    assert merged.project == "Wrong project"
    assert merged.desired_outcome == "Revised budget is agreed with Taras"
    assert merged.estimated_minutes == 20
    assert merged.deadline == date(2026, 9, 2)
    wiped = merge_drafts(merged, TaskDraft())
    assert wiped.project == "Wrong project"
    assert wiped.estimated_minutes == 20


def test_follow_up_groups_missing_fields() -> None:
    missing = (
        RequiredField.TASK_TITLE,
        RequiredField.DESIRED_OUTCOME,
        RequiredField.PROJECT,
        RequiredField.DEADLINE,
        RequiredField.ESTIMATED_MINUTES,
    )
    questions = follow_up_questions(missing, last_user_text="Please handle this")
    assert len(questions) == 4
    assert "SMART" in questions[0]
    uk = follow_up_questions((RequiredField.PROJECT,), last_user_text="Зроби це")
    assert uk == ["Для якого проєкту це?"]


def test_follow_up_asks_only_still_missing_outcome() -> None:
    questions = follow_up_questions(
        (RequiredField.DESIRED_OUTCOME,),
        last_user_text="Погодь бюджет з Тарасом",
    )
    assert questions == ["Який результат вважається готовим?"]
    assert "конкретне завдання" not in questions[0]


def test_date_only_reply_keeps_ukrainian_deadline_question() -> None:
    draft = TaskDraft(
        task_title="Поговорити з Андрієм Жовтенецьким про бюджет",
        project="Фундації УКУ за кордоном",
        people=("Андрій Жовтенецький",),
        estimated_minutes=60,
        desired_outcome="Є розуміння бюджетного процесу",
    )
    questions = follow_up_questions(
        (RequiredField.DEADLINE,),
        last_user_text="30.09.26",
        draft=draft,
        previous_language="uk",
    )
    assert questions == ["Який дедлайн (дата)?"]
    assert "What is the deadline" not in questions[0]


def test_confirmation_card_is_ukrainian() -> None:
    card = format_confirmation_card(
        complete_draft(
            project="Фундації УКУ за кордоном",
            people=("Андрій Жовтенецький",),
            task_title="Поговорити з Андрієм про бюджет",
            desired_outcome="Є розуміння бюджетного процесу",
        )
    )
    assert "📋 Задача" in card
    assert "Проєкт:" in card
    assert "Готово створити цю задачу?" in card
    assert "Ready to create this task" not in card
    assert "Task created" not in card


async def test_intake_keeps_ukrainian_after_numeric_deadline_reply() -> None:
    repo = FakeTaskRepository(known_projects=("Фундації УКУ за кордоном",))
    incomplete = TaskDraft(
        task_title="Поговорити з Андрієм Жовтенецьким про бюджет",
        project="Фундації УКУ за кордоном",
        people=("Андрій Жовтенецький",),
        estimated_minutes=60,
        desired_outcome="Є розуміння бюджетного процесу",
    )
    interpreter = ScriptedInterpreter([incomplete, incomplete])
    service = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repo)
    first = await service.handle_user_text(
        1,
        "Так, додай задачу поговорити з Андрієм про бюджет для фундації",
        chat_id=10,
    )
    assert first.kind == IntakeKind.FOLLOW_UP
    assert first.text == "Який дедлайн (дата)?"
    second = await service.handle_user_text(1, "30.09.26", chat_id=10)
    assert "What is the deadline" not in second.text
    if second.kind == IntakeKind.FOLLOW_UP:
        assert second.text == "Який дедлайн (дата)?"
    else:
        assert second.kind == IntakeKind.CONFIRMATION
        assert "📋 Задача" in second.text
        assert "Готово створити цю задачу?" in second.text


def test_confirmation_card_matches_expected_layout() -> None:
    card = format_confirmation_card(complete_draft())
    assert "📋 Задача" in card
    assert "Проєкт: Unity Center" in card
    assert "Задача: Agree the revised budget with Taras" in card
    assert "Люди: Taras" in card
    assert "Дедлайн: 2026-09-02" in card
    assert "Importance" not in card
    assert "Urgency" not in card
    assert "High" not in card
    assert "Urgent" not in card
    assert "Оцінка часу: 20 хв" in card
    assert "Результат: Revised budget is agreed" in card
    assert "Готово створити цю задачу? Так / Редагувати / Скасувати" in card


async def test_intake_asks_then_confirms_then_validates_without_db() -> None:
    interpreter = ScriptedInterpreter(
        [
            TaskDraft(
                task_title="Agree the revised budget with Taras",
                people=("Taras",),
                desired_outcome="Revised budget is agreed",
            ),
            complete_draft(),
        ]
    )
    service = make_intake(interpreter)
    first = await service.handle_user_text(1, "Agree the budget with Taras", chat_id=10)
    assert first.kind == IntakeKind.FOLLOW_UP
    assert "project" in first.text.lower() or "deadline" in first.text.lower() or "I still need" in first.text
    assert "importance" not in first.text.lower()
    assert "urgency" not in first.text.lower()

    second = await service.handle_user_text(
        1, "Unity Center, 2 Sep, 20 min", chat_id=10
    )
    assert second.kind == IntakeKind.CONFIRMATION
    assert second.show_confirm_buttons
    assert "Unity Center" in second.text

    yes = await service.handle_user_text(1, "Yes", chat_id=10)
    assert yes.kind == IntakeKind.CREATED
    assert "✅ Задачу створено" in yes.text
    assert "Unity Center" in yes.text


async def test_incomplete_extraction_never_ready() -> None:
    interpreter = ScriptedInterpreter([TaskDraft(task_title="Call someone tomorrow")])
    service = make_intake(interpreter)
    result = await service.handle_user_text(7, "Call someone tomorrow", chat_id=7)
    assert result.kind == IntakeKind.FOLLOW_UP
    assert result.show_confirm_buttons is False


async def test_clarification_supplies_missing_outcome_and_does_not_ask_again() -> None:
    first_draft = complete_draft(desired_outcome=None)
    interpreter = ScriptedInterpreter(
        [
            first_draft,
            TaskDraft(),
        ]
    )
    service = make_intake(interpreter)
    first = await service.handle_user_text(
        3,
        "Agree the revised budget with Taras for Unity Center by 2026-09-02, 20 min",
        chat_id=30,
    )
    assert first.kind == IntakeKind.FOLLOW_UP
    assert "результат" in first.text.lower() or "done look like" in first.text.lower()
    assert "конкретне завдання" not in first.text

    second = await service.handle_user_text(
        3, "Revised budget is agreed with Taras", chat_id=30
    )
    assert second.kind == IntakeKind.CONFIRMATION
    assert "Revised budget is agreed with Taras" in second.text
    assert "Agree the revised budget with Taras" in second.text
    assert "What does done look like" not in second.text
    assert "Який результат" not in second.text
    stored = interpreter.calls[1]
    assert stored["current_draft"] is not None
    assert RequiredField.DESIRED_OUTCOME in stored["missing"]


async def test_clarification_fills_multiple_fields_across_turns() -> None:
    interpreter = ScriptedInterpreter(
        [
            TaskDraft(
                task_title="Agree the revised budget with Taras",
                people=("Taras",),
                desired_outcome="Revised budget is agreed",
            ),
            TaskDraft(project="Unity Center"),
            TaskDraft(
                deadline=date(2026, 9, 2),
                estimated_minutes=20,
            ),
        ]
    )
    service = make_intake(interpreter)
    first = await service.handle_user_text(4, "Agree the revised budget with Taras", chat_id=40)
    assert first.kind == IntakeKind.FOLLOW_UP
    assert "project" in first.text.lower() or "проєкт" in first.text.lower()

    second = await service.handle_user_text(4, "Unity Center", chat_id=40)
    assert second.kind == IntakeKind.FOLLOW_UP
    assert "Unity Center" not in format_follow_up_probe(second.text)
    assert "deadline" in second.text.lower() or "дедлайн" in second.text.lower()
    assert "project" not in second.text.lower() and "проєкт" not in second.text.lower()

    third = await service.handle_user_text(4, "2 Sep, 20 min", chat_id=40)
    assert third.kind == IntakeKind.CONFIRMATION
    assert "Unity Center" in third.text
    assert "Agree the revised budget with Taras" in third.text
    assert "Taras" in third.text
    assert "2026-09-02" in third.text


def format_follow_up_probe(text: str) -> str:
    return text.casefold()


def test_fill_missing_from_user_reply_only_touches_asked_fields() -> None:
    base = complete_draft(desired_outcome=None, project="Unity Center")
    filled = fill_missing_from_user_reply(
        base,
        "Revised budget is agreed with the team",
        (RequiredField.DESIRED_OUTCOME,),
    )
    assert filled.project == "Unity Center"
    assert filled.task_title == base.task_title
    assert filled.desired_outcome == "Revised budget is agreed with the team"
    assert is_ready(filled)
