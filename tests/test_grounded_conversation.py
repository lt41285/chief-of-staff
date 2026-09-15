from collections.abc import AsyncIterator
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.models.task import Importance, Urgency
from chief_of_staff.models.utterance_intent import GroundedReply, RouterKind, UtteranceInterpretation
from chief_of_staff.services.conversation_context import InMemoryConversationStore
from chief_of_staff.services.date_windows import resolve_period
from chief_of_staff.services.grounded_reply import validate_grounded_reply
from chief_of_staff.services.person_relevance import structured_related, task_related_to_person
from chief_of_staff.services.task_math import fit_tasks_into_minutes, sum_estimated_minutes
from chief_of_staff.services.task_query import QueryKind, QueryResult
from tests.test_ai_router import ScriptedRouter, _clock, _turn
from tests.test_task_persistence import persist
from tests.test_task_validation import complete_draft


def _task(**overrides: object) -> PlanCandidate:
    data: dict[str, object] = {
        "id": uuid4(),
        "title": "Task",
        "project": "BG",
        "people": (),
        "deadline": date(2026, 9, 10),
        "importance": Importance.MEDIUM,
        "urgency": Urgency.NOT_URGENT,
        "estimated_minutes": 15,
        "status": "next",
    }
    data.update(overrides)
    return PlanCandidate(**data)  # type: ignore[arg-type]


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.fixture
def repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> SqlAlchemyTaskRepository:
    return SqlAlchemyTaskRepository(session_factory)


def test_next_week_is_14_to_20_september_2026() -> None:
    window = resolve_period("next_week", date(2026, 9, 8))
    assert window == (date(2026, 9, 14), date(2026, 9, 20))
    assert resolve_period("наступного тижня", date(2026, 9, 8)) == window


def test_sum_estimated_minutes_is_exact() -> None:
    tasks = [
        _task(estimated_minutes=5),
        _task(estimated_minutes=15),
        _task(estimated_minutes=15),
    ]
    summary = sum_estimated_minutes(tasks)
    assert summary.total_minutes == 35
    assert summary.missing == 0
    fitted, used = fit_tasks_into_minutes(tasks, 60)
    assert len(fitted) == 3
    assert used == 35


def test_borovets_eligibility_rejects_unrelated_titles() -> None:
    borovets = _task(title="Погодити графік", people=("Андрій Боровець",))
    zenia = _task(title="Взнати для Зені Кушпета контактну особу в банку")
    khoma = _task(title="Поговорити з Хомою про політику використання відсотків")
    routes = _task(title="Внести зміни в маршрути погодження онлайн заявок")
    named = _task(title="Написати Боровцю короткий апдейт", people=())
    assert structured_related(borovets, "Андрій Боровець")
    assert task_related_to_person(named, "Боровцем")
    assert not task_related_to_person(borovets, "Хома")
    assert not task_related_to_person(zenia, "Боровець")
    assert not task_related_to_person(khoma, "Боровець")
    assert not task_related_to_person(routes, "Боровець")


def test_grounded_reply_rejects_unknown_ids() -> None:
    known = uuid4()
    result = QueryResult(
        kind=QueryKind.TASKS,
        text="facts",
        task_ids=(known,),
        total_estimated_minutes=35,
    )
    bad = GroundedReply(
        message="Є 6 задач, десь 40–90 хвилин",
        referenced_task_ids=[str(uuid4())],
        stated_count=6,
        stated_total_minutes=90,
    )
    assert validate_grounded_reply(bad, result) is None
    good = GroundedReply(
        message="За твоїми оцінками — 35 хвилин.",
        referenced_task_ids=[str(known)],
        stated_count=1,
        stated_total_minutes=35,
    )
    assert validate_grounded_reply(good, result) == good.message


async def test_grounded_conversation_regression(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Поговорити з Хомою про політику використання відсотків по ґрантах",
            people=("Хома",),
            deadline=date(2026, 10, 7),
            estimated_minutes=20,
        ),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Взнати для Зені Кушпета контактну особу в банку",
            people=("Зеня Кушпета",),
            deadline=date(2026, 9, 12),
            estimated_minutes=40,
        ),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Внести зміни в маршрути погодження онлайн заявок",
            people=(),
            deadline=date(2026, 9, 11),
            estimated_minutes=90,
        ),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Узгодити порядок денний з Боровцем",
            people=("Андрій Боровець",),
            deadline=date(2026, 9, 10),
            estimated_minutes=5,
        ),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Надіслати Боровцю короткий статус",
            people=(),
            deadline=date(2026, 9, 16),
            estimated_minutes=15,
            desired_outcome="Боровець має статус",
        ),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Дочекатися відповіді від Боровця по датах",
            people=(),
            deadline=date(2026, 9, 18),
            estimated_minutes=15,
        ),
        user=1,
        chat=1,
    )
    listed = await repository.list_user_tasks(1)
    wait = next(task for task in listed if "відповіді від Боровця" in task.title)
    await repository.set_user_task_waiting(1, wait.id, "Андрій Боровець")
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Прострочений звіт для ради",
            people=(),
            deadline=date(2026, 9, 1),
            estimated_minutes=30,
        ),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Підготувати тижневий звіт",
            people=(),
            deadline=date(2026, 9, 15),
            estimated_minutes=25,
        ),
        user=1,
        chat=1,
    )

    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.GENERAL_CHAT),
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Хома"),
            UtteranceInterpretation(
                kind=RouterKind.PEOPLE_TASKS_QUERY,
                person_query="Боровець",
                available_minutes=60,
                advice=True,
            ),
            UtteranceInterpretation(
                kind=RouterKind.PEOPLE_TASKS_QUERY,
                person_query="Боровець",
            ),
            UtteranceInterpretation(kind=RouterKind.SUM_ESTIMATES, use_listed_ids=True),
            UtteranceInterpretation(kind=RouterKind.INSPECT_LISTED, use_listed_ids=True),
            UtteranceInterpretation(
                kind=RouterKind.REFINE_PREVIOUS,
                exclude_indexes=[1, 6],
                use_listed_ids=True,
            ),
            UtteranceInterpretation(kind=RouterKind.SUM_ESTIMATES, use_listed_ids=True),
            UtteranceInterpretation(kind=RouterKind.LIST_PERIOD, period="next_week"),
            UtteranceInterpretation(
                kind=RouterKind.REFINE_PREVIOUS,
                exclude_overdue=True,
            ),
        ]
    )

    greet, _, store = await _turn(repository, "привіт", router=router, context=store)
    assert greet.kind == QueryKind.INFO
    assert "не зовсім зрозумів" not in greet.text.casefold()
    assert "привіт" in greet.text.casefold()

    khoma, _, store = await _turn(repository, "що там по Хомі?", router=router, context=store)
    assert "Хомою" in khoma.text or "політик" in khoma.text.casefold()
    assert "Кушпета" not in khoma.text
    assert "маршрути погодження" not in khoma.text

    hour, _, store = await _turn(
        repository,
        "Маю зараз вільну годину. Що можу зробити, щоби закрити питання з Боровцем",
        router=router,
        context=store,
    )
    assert "Кушпета" not in hour.text
    assert "Хомою" not in hour.text
    assert "маршрути погодження" not in hour.text
    assert "35" in hour.text
    assert "40–90" not in hour.text and "40-90" not in hour.text
    borovets_titles = (
        "Узгодити порядок денний з Боровцем",
        "Надіслати Боровцю короткий статус",
        "Дочекатися відповіді від Боровця по датах",
    )
    for title in borovets_titles:
        assert title in hour.text
    assert hour.total_estimated_minutes == 35

    again, _, store = await _turn(
        repository,
        "А щоби закрити питання з Боровцем?",
        router=router,
        context=store,
    )
    for title in borovets_titles:
        assert title in again.text
    assert "Кушпета" not in again.text

    minutes, _, store = await _turn(
        repository, "скільки це все займе часу?", router=router, context=store
    )
    assert "35" in minutes.text
    assert minutes.total_estimated_minutes == 35

    which, _, store = await _turn(repository, "які саме задачі?", router=router, context=store)
    assert set(which.task_ids) == set(again.task_ids)
    for title in borovets_titles:
        assert title in which.text

    corrected, _, store = await _turn(
        repository,
        "Задачі 1 і 6 не пов'язані з Боровцем!",
        router=router,
        context=store,
    )
    assert "уточни" not in corrected.text.casefold()
    assert "яку саме" not in corrected.text.casefold()
    assert "Кушпета" not in corrected.text

    estimates, _, store = await _turn(
        repository,
        "Скільки часу на них піде, за моїми попередніми оцінками?",
        router=router,
        context=store,
    )
    assert "40–90" not in estimates.text
    assert str(estimates.total_estimated_minutes) in estimates.text
    assert estimates.total_estimated_minutes is not None

    week, _, store = await _turn(
        repository, "Що мені треба зробити наступного тижня?", router=router, context=store
    )
    assert "14 вересня" in week.text and "20 вересня" in week.text
    assert "Підготувати тижневий звіт" in week.text
    assert "Прострочений звіт для ради" not in week.text.split("Окремо")[0]
    assert "прострочен" in week.text.casefold()

    refined, _, _ = await _turn(
        repository,
        "Якщо задачі прострочені, то їх не слід відкладати на наступний тиждень",
        router=router,
        context=store,
    )
    assert "уточни" not in refined.text.casefold()
    assert "яку саме" not in refined.text.casefold()
    assert "Прострочений звіт для ради" not in refined.text.split("Окремо")[0]
    assert "Підготувати тижневий звіт" in refined.text
